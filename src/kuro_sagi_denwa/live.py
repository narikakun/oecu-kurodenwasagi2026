"""GPT-LiveのプライマリWebSocket接続を扱う。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any

from .audio import AudioDevice
from .config import Settings
from .scenario import FraudScenario
from .transcript import TranscriptLogger

logger = logging.getLogger(__name__)


class GPTLiveSession:
    URL = "wss://api.openai.com/v1/live/sessions"
    AI_HANGUP_PHRASE = "それでは、失礼します。"

    def __init__(
        self,
        settings: Settings,
        audio: AudioDevice,
        scenario: FraudScenario | None = None,
    ) -> None:
        self.settings = settings
        self.audio = audio
        self._fixed_scenario = scenario is not None
        self.scenario = scenario or FraudScenario()
        self._event_sequence = 0
        self.transcripts = TranscriptLogger(
            flush_seconds=settings.transcript_flush_ms / 1000,
            on_flush=self._remember_transcript,
        )
        self._history: list[dict[str, Any]] = []
        self._role = "test" if settings.conversation_test_mode else "service"
        self._assistant_transcript_tail = ""
        self._handoff_transcript_tail = ""
        self._ai_hangup_task: asyncio.Task[None] | None = None
        self._handoff_task: asyncio.Task[None] | None = None
        self._waiting_for_police_audio = False
        self._initial_silence_task: asyncio.Task[None] | None = None
        self._greeting_timer_started = False
        self._initial_retry_sent = False
        self._retry_resume_sent = False
        self._reply_watchdog_task: asyncio.Task[None] | None = None
        self._audio_control_waiters: dict[str, asyncio.Future[None]] = {}
        self._unmute_task: asyncio.Task[None] | None = None
        self._police_greeting_task: asyncio.Task[None] | None = None
        self._service_greeting_task: asyncio.Task[None] | None = None
        self._waiting_for_service_greeting_audio = False
        self._user_response_task: asyncio.Task[None] | None = None
        self._guardrail_task: asyncio.Task[None] | None = None
        self._state_transcript_tail = ""
        self._dial_enabled = False
        self._awaiting_final_decision = False
        self._police_greeting_spoken = False
        self._scam_suspicion_count = 0
        self._police_stage = 0
        self.websocket: Any = None
        self._sender_task: asyncio.Task[None] | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._closed_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self.websocket is not None

    async def start(self) -> None:
        if self.is_running:
            return

        # 通常運用では、受付番号を通話ごとに作り直す。
        # テストなどでscenarioを明示した場合だけ固定値を使う。
        if not self._fixed_scenario:
            self.scenario = FraudScenario()
        self._assistant_transcript_tail = ""
        self._handoff_transcript_tail = ""
        self._history = []
        self._role = "test" if self.settings.conversation_test_mode else "service"
        self._ai_hangup_task = None
        self._handoff_task = None
        self._waiting_for_police_audio = False
        self._initial_silence_task = None
        self._greeting_timer_started = False
        self._initial_retry_sent = False
        self._retry_resume_sent = False
        self._reply_watchdog_task = None
        self._audio_control_waiters = {}
        self._unmute_task = None
        self._police_greeting_task = None
        self._service_greeting_task = None
        self._waiting_for_service_greeting_audio = False
        self._user_response_task = None
        self._guardrail_task = None
        self._state_transcript_tail = ""
        self._dial_enabled = False
        self._awaiting_final_decision = False
        self._police_greeting_spoken = False
        self._scam_suspicion_count = 0
        self._police_stage = 0

        try:
            await self.audio.start()
            await self._open_live_session(
                "test" if self.settings.conversation_test_mode else "service",
                self.settings.live_voice,
                start_audio_sender=False,
            )
            # 最初の「もしもし」を参加者の音声に遮らせない。
            self.audio.clear_input_queue()
            await self._set_input_muted(True)
            self._sender_task = asyncio.create_task(self._send_audio_loop())
        except Exception:
            await self.audio.stop()
            if self.websocket is not None:
                await self.websocket.close()
                self.websocket = None
            raise

        self._waiting_for_service_greeting_audio = True
        await self.append_instructions(
            self._initial_greeting_instruction(),
            event_id=self._next_event_id("test_greeting")
            if self.settings.conversation_test_mode
            else self._next_event_id("scenario_greeting"),
        )
        self._service_greeting_task = asyncio.create_task(
            self._ensure_service_greeting()
        )
        logger.info("GPT-Liveの会話を開始しました")

    async def _open_live_session(
        self, role: str, voice: str, start_audio_sender: bool = True
    ) -> None:
        """指定した担当用の新しいGPT-Liveセッションを開始する。"""
        import websockets

        logger.info("GPT-Liveへ接続します（担当=%s、音声=%s）", role, voice)
        self._role = role
        self._assistant_transcript_tail = ""
        self._handoff_transcript_tail = ""
        self._state_transcript_tail = ""
        self._closed_event.clear()
        self.websocket = await websockets.connect(
            self.URL,
            additional_headers={"Authorization": f"Bearer {self.settings.api_key}"},
            max_size=None,
        )
        await self.websocket.send(json.dumps(self._session_start_event(role, voice)))
        await self._wait_until_started()
        self._receiver_task = asyncio.create_task(self._receive_loop())
        if start_audio_sender:
            self._sender_task = asyncio.create_task(self._send_audio_loop())

    def _session_start_event(
        self, role: str = "service", voice: str | None = None
    ) -> dict[str, Any]:
        selected_voice = voice or self.settings.live_voice
        return {
            "type": "session.start",
            "event_id": "black_phone_start",
            "session": {
                "model": self.settings.live_model,
                "instructions": self._session_instructions(role),
                "input": self._session_input(role),
                "audio": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": self.settings.sample_rate,
                    },
                    "output": {"voice": selected_voice},
                },
                # 初期版では外部ツールを使わず、音声会話だけに絞る。
                "delegation": {"type": "client"},
                "store": False,
            },
        }

    def _session_instructions(self, role: str) -> str:
        if not self.settings.conversation_test_mode:
            return self.scenario.build_instructions(
                self.settings.live_instructions, role=role
            )

        instructions = (
            "あなたは黒電話の音声入出力を確認するための会話相手です。"
            "決められた役割やシナリオはありません。相手の話題に合わせて自然な日本語で"
            "自由に会話してください。一回の発話は短くし、相手の返答を待ってください。"
            "実在人物になりすましたり、個人情報を尋ねたりしないでください。"
        )
        if self.settings.live_instructions:
            instructions += "\n\n# 追加設定\n" + self.settings.live_instructions
        return instructions

    def _initial_greeting_instruction(self) -> str:
        if self.settings.conversation_test_mode:
            return (
                "今すぐ日本語で「もしもし。音声テストを始めます。何か話しかけて"
                "ください。」とだけ発話し、その後は相手の返答を待ってください。"
            )
        return self.scenario.greeting_instruction()

    def _session_input(self, role: str) -> list[dict[str, Any]]:
        """新しい担当へ、履歴とその扱い方を明示して渡す。"""
        history = list(self._history)
        if role != "police" or not history:
            return history

        handoff_note = {
            "type": "message",
            "role": "developer",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "以下のuserとassistantのメッセージは、通信担当から正式に"
                        "引き継いだ直前の通話記録です。参加者がすでに答えた事実として"
                        "扱ってください。参加者に用件を最初から説明させたり、同じ質問を"
                        "やり直したりせず、警察担当の次の確認から続けてください。"
                    ),
                }
            ],
        }
        return [handoff_note, *history]

    async def append_instructions(self, content: str, event_id: str) -> None:
        """実行中の会話へ、アプリケーション側の指示を追加する。"""
        if not self.is_running:
            return
        assert self.websocket is not None
        event = {
            "type": "session.instructions.append",
            "event_id": event_id,
            "delegation_id": None,
            "content": content,
        }
        await self.websocket.send(json.dumps(event, ensure_ascii=False))

    async def _set_input_muted(self, muted: bool) -> None:
        """GPT-Liveの入力ミュートを変更し、サーバーの受付完了まで待つ。"""
        if not self.is_running:
            return
        assert self.websocket is not None
        action = "mute" if muted else "unmute"
        event_id = self._next_event_id(f"input_{action}")
        loop = asyncio.get_running_loop()
        accepted = loop.create_future()
        self._audio_control_waiters[event_id] = accepted
        await self.websocket.send(
            json.dumps(
                {"type": f"session.input_audio.{action}", "event_id": event_id}
            )
        )
        try:
            await asyncio.wait_for(accepted, timeout=3)
        finally:
            self._audio_control_waiters.pop(event_id, None)

    async def notify_dial(self, digit: int) -> None:
        """黒電話で回された数字をGPT-Liveの会話へ通知する。"""
        if self.settings.conversation_test_mode:
            logger.info("会話テストモードではダイヤル入力%dを使用しません", digit)
            return
        if not self._dial_enabled:
            logger.warning(
                "現在はダイヤル入力待ちではないため、%dは会話へ渡しません", digit
            )
            return
        await self.append_instructions(
            self.scenario.dial_instruction(digit),
            event_id=self._next_event_id(f"dial_{digit}"),
        )
        if digit == self.scenario.expected_digit:
            self._dial_enabled = False

    def _next_event_id(self, prefix: str) -> str:
        self._event_sequence += 1
        return f"{prefix}_{self._event_sequence}"

    async def _wait_until_started(self) -> None:
        assert self.websocket is not None
        while True:
            raw = await asyncio.wait_for(self.websocket.recv(), timeout=15)
            event = json.loads(raw)
            if event.get("type") == "session.started":
                return
            if event.get("type") == "error":
                message = event.get("error", {}).get("message", "不明なエラー")
                raise RuntimeError(f"GPT-Liveの開始に失敗しました: {message}")

    async def _send_audio_loop(self) -> None:
        assert self.websocket is not None
        while True:
            pcm = await self.audio.input_queue.get()
            event = {
                "type": "session.input_audio.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
            await self.websocket.send(json.dumps(event))

    async def _receive_loop(self) -> None:
        assert self.websocket is not None
        try:
            async for raw in self.websocket:
                event = json.loads(raw)
                event_type = event.get("type")

                if event_type == "session.output_audio.delta":
                    pcm = base64.b64decode(event["delta"])
                    if (
                        self._waiting_for_service_greeting_audio
                        and self._is_audible_pcm(pcm)
                    ):
                        self._waiting_for_service_greeting_audio = False
                        if self._service_greeting_task is not None:
                            self._service_greeting_task.cancel()
                            self._service_greeting_task = None
                        self._unmute_task = asyncio.create_task(
                            self._unmute_after_service_greeting()
                        )
                    if self._waiting_for_police_audio and self._is_audible_pcm(pcm):
                        self._waiting_for_police_audio = False
                        if self._police_greeting_task is not None:
                            self._police_greeting_task.cancel()
                            self._police_greeting_task = None
                        await self.audio.stop_hold_music()
                        logger.info("警察担当の第一声を受信したため保留音を停止しました")
                        self._unmute_task = asyncio.create_task(
                            self._unmute_after_police_greeting()
                        )
                    if (
                        not self._waiting_for_service_greeting_audio
                        and not self._waiting_for_police_audio
                    ):
                        # 音声チャンクだけでは「新しい返答」か直前発話の末尾かを
                        # 判別できない。応答監視は文字起こしが届くまで解除しない。
                        await self.audio.play(pcm)
                elif event_type == "session.input_transcript.delta":
                    delta = event.get("delta", "")
                    self.transcripts.add("user", delta)
                    if delta.strip() and self._initial_silence_task is not None:
                        self._initial_silence_task.cancel()
                        self._initial_silence_task = None
                elif event_type == "session.output_transcript.delta":
                    delta = event.get("delta", "")
                    if delta.strip():
                        self._cancel_reply_watchdog()
                    self.transcripts.add("assistant", delta)
                    self._update_conversation_state(delta)
                    if self._detect_handoff(delta):
                        logger.info("警察担当への切替を開始します")
                        self._handoff_task = asyncio.create_task(
                            self._handoff_to_police()
                        )
                    elif self._detect_ai_hangup(delta):
                        logger.info("AI側の通話終了を検知しました")
                        self._ai_hangup_task = asyncio.create_task(
                            self._finish_from_ai()
                        )
                elif event_type == "session.closed":
                    self._closed_event.set()
                    return
                elif event_type in {
                    "session.input_audio.muted",
                    "session.input_audio.unmuted",
                }:
                    client_event_id = event.get("client_event_id", "")
                    waiter = self._audio_control_waiters.get(client_event_id)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(None)
                elif event_type == "error":
                    message = event.get("error", {}).get("message", "不明なエラー")
                    logger.error("GPT-Liveエラー: %s", message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("GPT-Liveの受信処理が終了しました")
        finally:
            await self.transcripts.flush_all()
            self._closed_event.set()

    def _detect_ai_hangup(self, delta: str) -> bool:
        """分割された文字起こしから、AI専用の終了文を検知する。"""
        if self.settings.conversation_test_mode:
            return False
        if self._ai_hangup_task is not None:
            return False
        self._assistant_transcript_tail = (
            self._assistant_transcript_tail + delta
        )[-100:]
        return self.AI_HANGUP_PHRASE in self._assistant_transcript_tail

    @staticmethod
    def _is_audible_pcm(pcm: bytes, threshold: int = 160) -> bool:
        """無音に近い先頭チャンクで保留音を止めないための簡易判定。"""
        if len(pcm) < 2:
            return False
        samples = memoryview(pcm[: len(pcm) // 2 * 2]).cast("h")
        return any(abs(sample) >= threshold for sample in samples)

    def _detect_handoff(self, delta: str) -> bool:
        """通信担当の転送文を、文字起こしが分割されても検知する。"""
        if self.settings.conversation_test_mode:
            return False
        if self._role != "service" or self._handoff_task is not None:
            return False
        self._handoff_transcript_tail = (
            self._handoff_transcript_tail + delta
        )[-100:]
        return self.scenario.TRANSFER_PHRASE in self._handoff_transcript_tail

    def _update_conversation_state(self, delta: str) -> None:
        """AIの発話から、アプリ側で守るべき会話状態を更新する。"""
        if self.settings.conversation_test_mode:
            return
        self._state_transcript_tail = (self._state_transcript_tail + delta)[-240:]
        compact = self._state_transcript_tail.replace(" ", "")

        if "ダイヤル" in compact and "回して" in compact:
            self._dial_enabled = True
        if "安全確認用の口座" in compact and "移す" in compact:
            self._awaiting_final_decision = True
        if "お電話代わりました" in compact and "犯罪対策" in compact:
            self._police_greeting_spoken = True
            self._police_stage = max(self._police_stage, 1)
        if "身に覚えのない契約" in compact:
            self._police_stage = max(self._police_stage, 2)
        if "知らない人に電話を貸した" in compact:
            self._police_stage = max(self._police_stage, 3)
        if "ここまでのお話は分かりますか" in compact:
            self._police_stage = max(self._police_stage, 4)
        if "今、お時間は取れますか" in compact:
            self._police_stage = max(self._police_stage, 5)
        if "ご家族や銀行" in compact and "話さないで" in compact:
            self._police_stage = max(self._police_stage, 6)

        forbidden = ("少し間を置きます", "少しお待ちください", "少々お待ちください")
        if any(phrase in compact for phrase in forbidden):
            self._state_transcript_tail = ""
            if self._guardrail_task is None:
                self._guardrail_task = asyncio.create_task(
                    self._recover_from_waiting_phrase()
                )

    def _remember_transcript(self, speaker: str, text: str) -> None:
        """次のセッションへ渡すため、確定した会話をテキストで保存する。"""
        role = "user" if speaker == "user" else "assistant"
        content_type = "input_text" if role == "user" else "output_text"
        self._history.append(
            {
                "type": "message",
                "role": role,
                "content": [{"type": content_type, "text": text}],
            }
        )
        # 長時間の会話でも、新セッションには直近の発話だけを渡す。
        self._history = self._history[-40:]

        handoff_finished = self._handoff_task is None or self._handoff_task.done()
        if speaker == "user" and handoff_finished:
            self._start_reply_watchdog()
            if self.settings.conversation_test_mode:
                return
            if not (
                self._role == "service"
                and self._initial_retry_sent
                and not self._retry_resume_sent
            ):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    self._user_response_task = loop.create_task(
                        self._steer_after_user_reply(text)
                    )

        if self._role != "service":
            return

        # 5秒は接続開始時ではなく、最初の「もしもし」が実際に話し終わって
        # 文字起こしへ出た時点から数える。接続や音声生成の待ち時間を含めない。
        if (
            speaker == "assistant"
            and not self._greeting_timer_started
            and "もしもし" in text
        ):
            self._greeting_timer_started = True
            self._initial_silence_task = asyncio.create_task(
                self._prompt_after_initial_silence()
            )

        # 再呼びかけと参加者の返答がすれ違った場合でも、待ち状態のままにせず
        # 通信担当の自己紹介へ明示的に進める。
        if speaker == "user" and self._initial_retry_sent and not self._retry_resume_sent:
            self._retry_resume_sent = True
            self._cancel_reply_watchdog()
            asyncio.create_task(self._resume_after_initial_retry())

    async def _steer_after_user_reply(self, text: str) -> None:
        """重要な分岐だけはプロンプト任せにせず、アプリ側から明示する。"""
        compact = text.replace(" ", "")
        safety_words = (
            "家族",
            "相談",
            "警察に確認",
            "警察へ確認",
            "かけ直",
            "切ります",
            "断ります",
        )

        if any(word in compact for word in safety_words):
            self._awaiting_final_decision = False
            await self.append_instructions(
                self.scenario.safe_reflection_instruction(),
                event_id=self._next_event_id("safe_reflection"),
            )
            return

        if "詐欺" in compact:
            self._scam_suspicion_count += 1
            if self._scam_suspicion_count >= 2:
                await self.append_instructions(
                    self.scenario.safe_reflection_instruction(),
                    event_id=self._next_event_id("repeated_scam_suspicion"),
                )
            else:
                await self.append_instructions(
                    "相手から詐欺ではないかと疑われました。保留せず、今すぐ一度だけ"
                    "「ご不安になりますよね。ただ、今確認を止めると、確認が間に合わない"
                    "可能性があります。このまま確認を続けてもよろしいですか。」と話し、"
                    "返答を待ってください。",
                    event_id=self._next_event_id("scam_suspicion"),
                )
            return

        if self._awaiting_final_decision:
            self._awaiting_final_decision = False
            await self.append_instructions(
                self.scenario.danger_reflection_instruction(),
                event_id=self._next_event_id("danger_reflection"),
            )
            return

    async def _recover_from_waiting_phrase(self) -> None:
        try:
            await self.append_instructions(
                "待つ、保留する、間を置くという発話は禁止です。今すぐ現在の段階で必要な"
                "返答または振り返りを一つだけ話し、会話を進めてください。",
                event_id=self._next_event_id("waiting_phrase_guardrail"),
            )
        finally:
            self._guardrail_task = None

    def _start_reply_watchdog(self) -> None:
        """参加者への返答が止まった場合に、会話を再開させる。"""
        self._cancel_reply_watchdog()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 通常は音声受信ループ内から呼ばれる。同期テストなどでは監視しない。
            return
        self._reply_watchdog_task = loop.create_task(self._prompt_reply_after_timeout())

    def _cancel_reply_watchdog(self) -> None:
        task = self._reply_watchdog_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._reply_watchdog_task = None

    async def _prompt_reply_after_timeout(self) -> None:
        try:
            timeout = self.settings.turn_reply_timeout_ms / 1000
            await asyncio.sleep(timeout)
            logger.warning("参加者への返答がないため、GPT-Liveへ再応答を指示します")
            if self.settings.conversation_test_mode:
                content = (
                    "音声テスト中に相手が返答しましたが、こちらの応答が止まっています。"
                    "相手の直前の発言に対して、自然な短い返答を今すぐ話してください。"
                )
            elif self._role == "police":
                content = self.scenario.police_next_instruction(self._police_stage)
            else:
                content = (
                    "相手が返答した後、こちらの応答が止まっています。相手の直前の発言を"
                    "意味どおりに受け止め、すでに答えた質問は飛ばしてください。"
                    "通信担当として、自然な短い返答か次の質問を一つだけ今すぐ話してください。"
                )
            await self.append_instructions(
                content,
                event_id=self._next_event_id("turn_reply_prompt"),
            )
            await asyncio.sleep(timeout)
            logger.warning("再応答がないため、GPT-Liveへもう一度指示します")
            await self.append_instructions(
                content,
                event_id=self._next_event_id("turn_reply_retry"),
            )
        except asyncio.CancelledError:
            return
        finally:
            if self._reply_watchdog_task is asyncio.current_task():
                self._reply_watchdog_task = None

    async def _resume_after_initial_retry(self) -> None:
        if self.settings.conversation_test_mode:
            await self.append_instructions(
                "相手から返事がありました。音声テストとして、相手の発言へ自然に短く"
                "返答し、自由な会話を続けてください。",
                event_id=self._next_event_id("test_initial_retry_resume"),
            )
            return
        await self.append_instructions(
            "相手から返事があり、こちらの声も聞こえています。今すぐ"
            "「ありがとうございます。突然のお電話ですみません。通信サービスの確認担当です。」"
            "とだけ話し、その後は相手の返答を待ってください。",
            event_id=self._next_event_id("initial_retry_resume"),
        )

    async def _handoff_to_police(self) -> None:
        """保留音を挟み、履歴と別の声を使う警察担当セッションへ切り替える。"""
        try:
            self._cancel_reply_watchdog()
            await asyncio.sleep(1.2)
            await self.audio.wait_until_played()
            await self.transcripts.flush_all()

            music_path = Path(__file__).parent / "assets" / "audio" / "transfer-hold.mp3"
            await self.audio.start_hold_music(
                str(music_path), self.settings.hold_music_volume
            )
            await self._close_live_connection()

            self._waiting_for_police_audio = True
            self.audio.clear_input_queue()
            await self._open_live_session(
                "police", self.settings.police_voice, start_audio_sender=False
            )
            logger.info(
                "警察担当へ通信担当の会話履歴%d件を引き継ぎました", len(self._history)
            )
            # 通信担当へ向けた最後の返答が、新しい担当の第一声を遮らないようにする。
            self.audio.clear_input_queue()
            await self._set_input_muted(True)
            self._sender_task = asyncio.create_task(self._send_audio_loop())
            await self.append_instructions(
                self.scenario.police_greeting_instruction(),
                event_id=self._next_event_id("police_greeting"),
            )
            self._police_greeting_task = asyncio.create_task(
                self._ensure_police_greeting()
            )
            logger.info("警察担当のGPT-Liveセッションを開始しました")
        except asyncio.CancelledError:
            await self.audio.stop_hold_music()
            raise
        except Exception:
            self._waiting_for_police_audio = False
            await self.audio.stop_hold_music()
            logger.exception("警察担当への切替に失敗しました")
        finally:
            # 完了したTaskを残すと、警察担当の発話まで「切替中」と誤判定してしまう。
            if self._handoff_task is asyncio.current_task():
                self._handoff_task = None

    async def _unmute_after_service_greeting(self) -> None:
        """通信担当が「もしもし」と話し始めた後、マイク入力を開始する。"""
        try:
            if self._sender_task is not None:
                self._sender_task.cancel()
                await asyncio.gather(self._sender_task, return_exceptions=True)
                self._sender_task = None
            self.audio.clear_input_queue()
            await self._set_input_muted(False)
            self._sender_task = asyncio.create_task(self._send_audio_loop())
            logger.info("最初の『もしもし』開始後、マイク入力を開始しました")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("最初の挨拶後のマイク入力開始に失敗しました")

    async def _ensure_service_greeting(self) -> None:
        """通信担当の最初の「もしもし」が出なければ再度指示する。"""
        try:
            timeout = self.settings.turn_reply_timeout_ms / 1000
            for retry in range(2):
                await asyncio.sleep(timeout)
                if not self._waiting_for_service_greeting_audio:
                    return
                logger.warning(
                    "最初の『もしもし』がないため再指示します（%d回目）", retry + 1
                )
                await self.append_instructions(
                    self._initial_greeting_instruction(),
                    event_id=self._next_event_id(f"service_greeting_retry_{retry + 1}"),
                )
        except asyncio.CancelledError:
            return
        finally:
            if self._service_greeting_task is asyncio.current_task():
                self._service_greeting_task = None

    async def _unmute_after_police_greeting(self) -> None:
        """警察担当が話し始めた後、参加者の音声入力を再開する。"""
        try:
            if self._sender_task is not None:
                self._sender_task.cancel()
                await asyncio.gather(self._sender_task, return_exceptions=True)
                self._sender_task = None
            self.audio.clear_input_queue()
            await self._set_input_muted(False)
            self._sender_task = asyncio.create_task(self._send_audio_loop())
            logger.info("警察担当の第一声開始後、マイク入力を再開しました")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("警察担当切替後のマイク入力再開に失敗しました")

    async def _ensure_police_greeting(self) -> None:
        """警察担当が自分から話し始めない場合、第一声を再度指示する。"""
        try:
            timeout = self.settings.turn_reply_timeout_ms / 1000
            for retry in range(2):
                await asyncio.sleep(timeout)
                if not self._waiting_for_police_audio:
                    return
                logger.warning(
                    "警察担当の第一声がないため再指示します（%d回目）", retry + 1
                )
                await self.append_instructions(
                    "相手の発話を待たないでください。今すぐ日本語で"
                    "「もしもし、お電話代わりました。犯罪対策を担当している者です。」"
                    "とだけ話し、その後に相手の返答を待ってください。",
                    event_id=self._next_event_id(f"police_greeting_retry_{retry + 1}"),
                )
        except asyncio.CancelledError:
            return
        finally:
            if self._police_greeting_task is asyncio.current_task():
                self._police_greeting_task = None

    async def _prompt_after_initial_silence(self) -> None:
        """最初の呼びかけに返事がなければ、アプリ側から再発話を指示する。"""
        try:
            await asyncio.sleep(self.settings.initial_silence_prompt_ms / 1000)
            self._initial_retry_sent = True
            content = (
                "相手からまだ返事がありません。今すぐ「もしもし、音声は聞こえますか。」"
                "とだけ話し、その後は相手の返答を待ってください。"
                if self.settings.conversation_test_mode
                else "相手からまだ返事がありません。今すぐ「もしもし、聞こえますでしょうか。」"
                "とだけ話し、その後は相手の返答を待ってください。"
            )
            await self.append_instructions(
                content,
                event_id=self._next_event_id("initial_silence_prompt"),
            )
        except asyncio.CancelledError:
            return
        finally:
            self._initial_silence_task = None

    async def _finish_from_ai(self) -> None:
        """AIの最後の音声を再生後、話中音を鳴らして終了する。"""
        # GPT-Liveには出力完了イベントがないため、最後の文字起こし後に
        # 少し待ち、届いた音声キューを再生し切ってから閉じる。
        await asyncio.sleep(1.2)
        await self.audio.wait_until_played()
        await self._close_live_connection()
        await self.audio.play_busy_tone(repeats=3)
        await self.audio.stop()

    async def _close_live_connection(self) -> None:
        if self.websocket is None:
            return

        if self._initial_silence_task is not None:
            self._initial_silence_task.cancel()
            await asyncio.gather(self._initial_silence_task, return_exceptions=True)
            self._initial_silence_task = None

        if self._sender_task is not None:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
            self._sender_task = None

        try:
            await self.websocket.send(json.dumps({"type": "session.close"}))
            await asyncio.wait_for(self._closed_event.wait(), timeout=3)
        except Exception as error:
            logger.debug("GPT-Liveの終了応答を待たずに切断します: %s", error)
        finally:
            if self._receiver_task is not None:
                self._receiver_task.cancel()
                await asyncio.gather(self._receiver_task, return_exceptions=True)
                self._receiver_task = None
            await self.websocket.close()
            self.websocket = None
            logger.info("GPT-Liveの会話を終了しました")

    async def stop(self) -> None:
        current_task = asyncio.current_task()
        self._cancel_reply_watchdog()
        for task_name in (
            "_service_greeting_task",
            "_police_greeting_task",
            "_unmute_task",
            "_user_response_task",
            "_guardrail_task",
        ):
            task = getattr(self, task_name)
            if task is not None and task is not current_task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                setattr(self, task_name, None)
        if self._handoff_task is not None and self._handoff_task is not current_task:
            self._handoff_task.cancel()
            await asyncio.gather(self._handoff_task, return_exceptions=True)
            self._handoff_task = None
        if not self.is_running:
            if self._ai_hangup_task is not None:
                self._ai_hangup_task.cancel()
                await asyncio.gather(self._ai_hangup_task, return_exceptions=True)
                self._ai_hangup_task = None
            await self.audio.stop()
            return

        if self._ai_hangup_task is not None and self._ai_hangup_task is not current_task:
            self._ai_hangup_task.cancel()
            await asyncio.gather(self._ai_hangup_task, return_exceptions=True)
            self._ai_hangup_task = None
        await self.audio.stop()
        await self._close_live_connection()
