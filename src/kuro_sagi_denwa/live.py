"""GPT-Liveとの最小限の双方向音声セッションを扱う。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any

from .archive import TranscriptArchive
from .audio import AudioDevice
from .config import Settings
from .scenario import build_refund_fraud_scenario

transcript_logger = logging.getLogger("kuro_sagi_denwa.transcript")
logger = logging.getLogger(__name__)

_KANJI_DIGITS = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_SMALL_NUMBER_UNITS = {"十": 10, "百": 100, "千": 1_000}
_LARGE_NUMBER_UNITS = {"万": 10_000, "億": 100_000_000, "兆": 1_000_000_000_000}
_KANJI_NUMBER_RE = re.compile(r"[0-9〇零一二三四五六七八九十百千万億兆]+")
_FULL_WIDTH_DIGIT_TABLE = str.maketrans("０１２３４５６７８９", "0123456789")


def normalize_transcript_numbers(text: str) -> str:
    """文字起こし中の全角数字と漢数字を半角数字へ統一する。"""
    normalized = text.translate(_FULL_WIDTH_DIGIT_TABLE)

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        has_unit = any(
            character in _SMALL_NUMBER_UNITS or character in _LARGE_NUMBER_UNITS
            for character in token
        )
        if not has_unit:
            return "".join(
                character if character.isascii() else str(_KANJI_DIGITS[character])
                for character in token
            )

        total = 0
        section = 0
        digits = ""
        for character in token:
            if character.isascii() or character in _KANJI_DIGITS:
                digits += (
                    character if character.isascii() else str(_KANJI_DIGITS[character])
                )
            elif character in _SMALL_NUMBER_UNITS:
                section += int(digits or "1") * _SMALL_NUMBER_UNITS[character]
                digits = ""
            else:
                section += int(digits or "0")
                total += (section or 1) * _LARGE_NUMBER_UNITS[character]
                section = 0
                digits = ""
        return str(total + section + int(digits or "0"))

    return _KANJI_NUMBER_RE.sub(replace, normalized)


def normalize_transcript_for_display(text: str, *, assistant: bool) -> str:
    """数字を整え、AIの発音用表記だけを字幕用の正式表記へ戻す。"""
    normalized = text
    if assistant:
        normalized = normalized.replace(
            "にまんにせんごひゃくろくじゅうえん", "22560円"
        )
        normalized = normalized.replace(
            "二万二千五百六十円", "22560円"
        )
        normalized = normalized.replace("もりぐち市役所", "守口市役所")
        normalized = normalized.replace("もりぐちしやくしょ", "守口市役所")
        normalized = normalized.replace("モリグチ市役所", "守口市役所")
    return normalize_transcript_numbers(normalized)


class GPTLiveSession:
    """1接続だけを使い、受話器とGPT-Liveの音声をそのまま中継する。"""

    URL = "wss://api.openai.com/v1/live/sessions"
    DEFAULT_INSTRUCTIONS = (
        "実際の電話のように、日本語で自然に会話してください。"
        "一度の発話は原則1〜2文にし、一度に複数の質問をせず、質問は1つだけにしてください。"
        "短い相づちや感想も使い、毎回質問で返さないでください。"
        "長い説明が必要な場合は短く区切り、相手の反応を待ってください。"
        "相手が話し始めたら発話を止め、最後まで聞いてください。"
        "通常の挨拶や簡単な質問にはすぐ答えてください。"
        "発音ルール: 『守口』の漢字をそのまま読まず、発話するときは毎回必ず"
        "『もりぐち』というひらがなの音へ置き換えてください。"
        "『まもりくち』という読み方は禁止です。『守口市役所』は必ず"
        "『もりぐちしやくしょ』と発音してください。"
        "『保険年金課』を『ほけんねんきんか』、『還付金』を『かんぷきん』、"
        "『医療費』を『いりょうひ』、『ATM』を『エーティーエム』と読んでください。"
        "『22,560円』は『にまんにせんごひゃくろくじゅうえん』と読んでください。"
    )
    GREETING_INSTRUCTION = (
        "相手の発言を待たず、今すぐあなたから先に日本語で「もしもし？」とだけ言ってください。"
        "ほかの言葉は加えず、言い終えたら相手の返事を待ってください。"
    )
    GREETING_RETRY_INSTRUCTION = (
        "まだ最初の挨拶が聞こえていません。今すぐ「もしもし？」とだけ、もう一度言ってください。"
        "言い終えたら相手の返事を待ってください。"
    )
    GREETING_TIMEOUT_SECONDS = 3
    CONTROL_ACK_TIMEOUT_SECONDS = 15
    FIRST_AUDIO_TIMEOUT_SECONDS = 1
    TRANSCRIPT_IDLE_SECONDS = 1.0
    TRANSCRIPT_SENTENCE_SECONDS = 0.15

    def __init__(
        self,
        settings: Settings,
        audio: AudioDevice,
        display: Any = None,
        archive: TranscriptArchive | None = None,
    ) -> None:
        self.settings = settings
        self.audio = audio
        self.display = display
        self.archive = archive
        self.websocket: Any = None
        self._sender_task: asyncio.Task[None] | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._closed_event = asyncio.Event()
        self._prepared = False
        self._transcript_buffers: dict[str, str] = {}
        self._transcript_flush_handles: dict[str, asyncio.TimerHandle] = {}
        self._reset_greeting_state()

    @property
    def is_running(self) -> bool:
        return self.websocket is not None

    @property
    def is_conversation_active(self) -> bool:
        return self._receiver_task is not None

    async def prepare(self) -> None:
        """ベル中に接続だけを済ませ、受話器を上げるまで入力をミュートする。"""
        if self._prepared and self.websocket is not None:
            return

        import websockets

        try:
            logger.info("GPT-Liveセッションへ接続します")
            self._closed_event.clear()
            self.websocket = await websockets.connect(
                self.URL,
                additional_headers={"Authorization": f"Bearer {self.settings.api_key}"},
                max_size=None,
            )
            await self._send_event(self._session_start_event())
            await self._wait_until_started()
            await self._send_event(
                {"type": "session.input_audio.mute", "event_id": "pre_ring_mute"}
            )
            await self._wait_for_ack("session.input_audio.muted", "pre_ring_mute")
            self._mark_greeting_requested("initial_greeting")
            await self._send_event(self._greeting_event())
            self._prepared = True
            logger.info("GPT-Liveセッションを開始しました（入力ミュート中）")
            logger.info("最初の「もしもし？」を先行準備します")
        except asyncio.CancelledError:
            logger.info("GPT-Liveセッションの開始を中止します")
            await self._abort_start()
            raise
        except Exception:
            logger.exception("GPT-Liveセッションを開始できませんでした")
            await self._abort_start()
            raise

    async def start(self) -> None:
        if self.is_conversation_active:
            return

        if self.archive is not None:
            self.archive.begin_call()
        try:
            await self.prepare()
            await self.audio.start()

            # マイクを先に準備し、サーバーのミュート解除を確認してから送信を始める。
            # 先行指示した挨拶の再生中も入力を継続する。
            self.audio.clear_input_queue()
            await self._send_event(
                {"type": "session.input_audio.unmute", "event_id": "handset_up_unmute"}
            )
            logger.info("GPT-Liveの入力ミュート解除を待ちます")
            await self._wait_for_ack(
                "session.input_audio.unmuted", "handset_up_unmute"
            )
            logger.info("GPT-Liveの入力ミュート解除を確認しました")
            await self._send_first_audio_packet()
            self._sender_task = asyncio.create_task(self._send_audio_loop())
            self._sender_task.add_done_callback(self._report_sender_failure)
            if not self._greeting_requested:
                self._mark_greeting_requested("initial_greeting")
                await self._send_event(self._greeting_event())
                logger.info("GPT-Liveへ最初の「もしもし？」を指示しました")
            await self._wait_until_greeting_accepted()

            # 挨拶後は通常の受信ループへ引き継ぐ。
            self._receiver_task = asyncio.create_task(self._receive_loop())
            self._receiver_task.add_done_callback(self._report_receiver_failure)
            logger.info("GPT-Liveの通話を開始しました")
        except asyncio.CancelledError:
            # 挨拶中に受話器を置かれた場合も、送信タスクや接続を残さない。
            await self._abort_start()
            if self.archive is not None:
                self.archive.end_call("cancelled")
            raise
        except Exception:
            await self._abort_start()
            if self.archive is not None:
                self.archive.end_call("error")
            raise

    def _session_start_event(self) -> dict[str, Any]:
        instructions = self.DEFAULT_INSTRUCTIONS + "\n\n" + build_refund_fraud_scenario()
        if self.settings.live_instructions:
            instructions += "\n\n" + self.settings.live_instructions

        responses: dict[str, Any] = {
            "model": self.settings.live_backend_model,
            "instructions": (
                "ユーザーの質問に日本語で正確かつ簡潔に答えてください。"
                "最新情報や事実確認が必要な質問ではWeb検索を使用してください。"
                "確認できなかった場合は、その点を明示してください。"
            ),
        }
        if self.settings.live_web_search:
            responses["tools"] = [{"type": "web_search"}]
            responses["tool_choice"] = "auto"

        return {
            "type": "session.start",
            "event_id": "black_phone_start",
            "session": {
                "model": self.settings.live_model,
                "instructions": instructions,
                "audio": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": self.settings.sample_rate,
                    },
                    "output": {"voice": self.settings.live_voice},
                },
                "delegation": {
                    "type": "responses",
                    "responses": responses,
                },
            },
        }

    def _greeting_event(self, event_id: str = "initial_greeting") -> dict[str, Any]:
        return {
            "type": "session.instructions.append",
            "event_id": event_id,
            "delegation_id": None,
            "content": (
                self.GREETING_RETRY_INSTRUCTION
                if event_id == "initial_greeting_retry"
                else self.GREETING_INSTRUCTION
            ),
        }

    def _reset_greeting_state(self) -> None:
        self._greeting_requested = False
        self._greeting_completed = False
        self._greeting_instruction_accepted = False
        self._greeting_audio_received = False
        self._greeting_transcript = ""
        self._greeting_event_ids: set[str] = set()

    def _mark_greeting_requested(self, event_id: str) -> None:
        self._greeting_requested = True
        self._greeting_event_ids.add(event_id)

    def _track_greeting_event(self, event: dict[str, Any]) -> None:
        if not self._greeting_requested or self._greeting_completed:
            return
        event_type = event.get("type")
        received_client_event_id = event.get("client_event_id")
        if event_type == "session.instructions.appended" and (
            received_client_event_id is None
            or received_client_event_id in self._greeting_event_ids
        ):
            self._greeting_instruction_accepted = True
        elif event_type == "session.output_audio.delta":
            self._greeting_audio_received = True
        elif event_type == "session.output_transcript.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                self._greeting_transcript += delta

    async def _send_event(self, event: dict[str, Any]) -> None:
        assert self.websocket is not None
        await self.websocket.send(json.dumps(event, ensure_ascii=False))

    def _log_transcript(self, event: dict[str, Any]) -> bool:
        """文字起こしの断片を蓄積し、文章単位でコンソールへ出す。"""
        labels = {
            "session.input_transcript.delta": "参加者",
            "session.output_transcript.delta": "AI",
        }
        label = labels.get(event.get("type"))
        delta = event.get("delta")
        if label is None or not isinstance(delta, str) or not delta:
            return False

        current = self._transcript_buffers.get(label, "")
        assistant = label == "AI"
        if not current:
            transcript_logger.info(
                "文字起こし中（%s）: %s",
                label,
                normalize_transcript_for_display(delta, assistant=assistant),
            )
        self._transcript_buffers[label] = current + delta
        if self.display is not None:
            self.display.publish(
                {
                    "type": "transcript",
                    "speaker": "user" if label == "参加者" else "assistant",
                    "status": "partial",
                    "text": normalize_transcript_for_display(
                        self._transcript_buffers[label], assistant=assistant
                    ),
                }
            )

        previous_handle = self._transcript_flush_handles.pop(label, None)
        if previous_handle is not None:
            previous_handle.cancel()

        delay = (
            self.TRANSCRIPT_SENTENCE_SECONDS
            if self._transcript_buffers[label].rstrip().endswith(("。", "！", "？", ".", "!", "?"))
            else self.TRANSCRIPT_IDLE_SECONDS
        )
        self._transcript_flush_handles[label] = asyncio.get_running_loop().call_later(
            delay, self._flush_transcript, label
        )
        return True

    def _flush_transcript(self, label: str) -> None:
        handle = self._transcript_flush_handles.pop(label, None)
        if handle is not None:
            handle.cancel()
        text = self._transcript_buffers.pop(label, "")
        if text:
            normalized_text = normalize_transcript_for_display(
                text, assistant=label == "AI"
            )
            transcript_logger.info("%s: %s", label, normalized_text)
            if self.archive is not None:
                self.archive.record_transcript(
                    "assistant" if label == "AI" else "user", normalized_text
                )
            if self.display is not None:
                self.display.publish(
                    {
                        "type": "transcript",
                        "speaker": "user" if label == "参加者" else "assistant",
                        "status": "final",
                        "text": normalized_text,
                    }
                )

    def _flush_all_transcripts(self) -> None:
        for label in list(self._transcript_buffers):
            self._flush_transcript(label)

    @staticmethod
    def _report_receiver_failure(task: asyncio.Task[None]) -> None:
        GPTLiveSession._report_task_failure(task, "GPT-Liveの受信処理が停止しました")

    @staticmethod
    def _report_sender_failure(task: asyncio.Task[None]) -> None:
        GPTLiveSession._report_task_failure(task, "GPT-Liveの音声送信が停止しました")

    @staticmethod
    def _report_task_failure(task: asyncio.Task[None], message: str) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                message,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def notify_dial(self, digit: int) -> None:
        """現在の最小構成ではダイヤルを会話制御に使用しない。"""

    async def _wait_until_started(self) -> None:
        assert self.websocket is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.CONTROL_ACK_TIMEOUT_SECONDS
        timeout_label = f"{self.CONTROL_ACK_TIMEOUT_SECONDS:g}秒以内"
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError(
                    f"GPT-Liveの開始確認が{timeout_label}に返りませんでした"
                )
            try:
                raw = await asyncio.wait_for(self.websocket.recv(), timeout=remaining)
            except asyncio.TimeoutError as error:
                raise RuntimeError(
                    f"GPT-Liveの開始確認が{timeout_label}に返りませんでした"
                ) from error
            event = json.loads(raw)
            logger.debug("GPT-Live開始待機イベント: %s", event.get("type"))
            if event.get("type") == "session.started":
                return
            if event.get("type") == "error":
                message = event.get("error", {}).get("message", "不明なエラー")
                raise RuntimeError(f"GPT-Liveの開始に失敗しました: {message}")

    async def _wait_for_ack(self, event_type: str, client_event_id: str) -> None:
        assert self.websocket is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.CONTROL_ACK_TIMEOUT_SECONDS
        timeout_label = f"{self.CONTROL_ACK_TIMEOUT_SECONDS:g}秒以内"
        last_event_type = "なし"
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError(
                    f"GPT-Liveから{event_type}が{timeout_label}に返りませんでした"
                    f"（最後のイベント: {last_event_type}）"
                )
            try:
                raw = await asyncio.wait_for(self.websocket.recv(), timeout=remaining)
            except asyncio.TimeoutError as error:
                raise RuntimeError(
                    f"GPT-Liveから{event_type}が{timeout_label}に返りませんでした"
                    f"（最後のイベント: {last_event_type}）"
                ) from error
            event = json.loads(raw)
            last_event_type = str(event.get("type", "不明"))
            logger.debug("GPT-Live制御待機イベント: %s", last_event_type)
            await self._handle_server_event(event)
            received_client_event_id = event.get("client_event_id")
            if event.get("type") == event_type and (
                received_client_event_id is None
                or received_client_event_id == client_event_id
            ):
                return

    async def _wait_until_greeting_accepted(self) -> None:
        """挨拶の受理、音声出力、文字起こしの「もしもし」を確認する。"""
        assert self.websocket is not None
        if not self._greeting_requested:
            self._mark_greeting_requested("initial_greeting")
        retried = False
        verified_by_audio_only = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.GREETING_TIMEOUT_SECONDS
        while not (
            self._greeting_instruction_accepted
            and self._greeting_audio_received
            and "もしもし" in self._greeting_transcript
        ):
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                raw = await asyncio.wait_for(
                    self.websocket.recv(), timeout=remaining
                )
            except asyncio.TimeoutError:
                if retried:
                    if (
                        self._greeting_instruction_accepted
                        and self._greeting_audio_received
                    ):
                        verified_by_audio_only = True
                        logger.warning(
                            "初回挨拶の字幕は届きませんでしたが、"
                            "指示受理と音声を確認できたため通話を開始します"
                        )
                        break
                    raise RuntimeError(
                        "GPT-Liveから初回挨拶「もしもし？」が返りませんでした"
                        f"（指示受理={self._greeting_instruction_accepted}、"
                        f"音声={self._greeting_audio_received}、"
                        f"文字起こし={bool(self._greeting_transcript)}）"
                    )
                retry_event_id = "initial_greeting_retry"
                self._mark_greeting_requested(retry_event_id)
                logger.warning(
                    "GPT-Liveの初回挨拶が届かないため、挨拶を再送します"
                    "（指示受理=%s、音声=%s、文字起こし=%s）",
                    self._greeting_instruction_accepted,
                    self._greeting_audio_received,
                    bool(self._greeting_transcript),
                )
                await self._send_event(self._greeting_event(retry_event_id))
                retried = True
                deadline = loop.time() + self.GREETING_TIMEOUT_SECONDS
                continue
            event = json.loads(raw)
            logger.debug("GPT-Live挨拶待機イベント: %s", event.get("type"))
            await self._handle_server_event(event)
        await self.audio.wait_until_played()
        self._greeting_completed = True
        if verified_by_audio_only:
            logger.info("GPT-Liveの初回挨拶を音声で確認しました（字幕なし）")
        else:
            logger.info(
                "GPT-Liveの初回挨拶を確認しました: %s",
                self._greeting_transcript,
            )

    async def _send_first_audio_packet(self) -> None:
        """受話器を上げた直後から入力タイムラインが動くことを保証する。"""
        try:
            pcm = await asyncio.wait_for(
                self.audio.input_queue.get(),
                timeout=self.FIRST_AUDIO_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as error:
            raise RuntimeError(
                "USB受話器から最初の音声データが届きませんでした"
            ) from error
        try:
            await self._send_audio_packet(pcm)
        finally:
            self.audio.input_queue.task_done()
        logger.info("GPT-Liveへの入力音声送信を開始しました")

    async def _send_audio_packet(self, pcm: bytes) -> None:
        await self._send_event(
            {
                "type": "session.input_audio.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        )

    async def _send_audio_loop(self) -> None:
        assert self.websocket is not None
        while True:
            pcm = await self.audio.input_queue.get()
            try:
                await self._send_audio_packet(pcm)
            finally:
                self.audio.input_queue.task_done()

    async def _receive_loop(self) -> None:
        assert self.websocket is not None
        try:
            async for raw in self.websocket:
                event = json.loads(raw)
                if await self._handle_server_event(event):
                    return
        except asyncio.CancelledError:
            raise
        finally:
            self._flush_all_transcripts()
            self._closed_event.set()

    async def _wait_until_closed(self) -> None:
        """受信ループ開始前の事前接続セッションを正常終了まで読み取る。"""
        assert self.websocket is not None
        while True:
            raw = await self.websocket.recv()
            event = json.loads(raw)
            if await self._handle_server_event(event):
                return

    async def _handle_server_event(self, event: dict[str, Any]) -> bool:
        """通常受信と開始時挨拶で共通のサーバーイベントを処理する。"""
        event_type = event.get("type")
        self._track_greeting_event(event)
        self._log_transcript(event)

        if event_type == "session.output_audio.delta":
            await self.audio.play(base64.b64decode(event["delta"]))
        elif event_type == "error":
            error = event.get("error", {})
            message = error.get("message", "不明なエラー")
            code = error.get("code")
            if code:
                raise RuntimeError(f"GPT-Liveエラー [{code}]: {message}")
            raise RuntimeError(f"GPT-Liveエラー: {message}")
        elif event_type == "session.closed":
            self._closed_event.set()
            return True
        return False

    async def _abort_start(self) -> None:
        if self._sender_task is not None:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
            self._sender_task = None
        if self._receiver_task is not None:
            self._receiver_task.cancel()
            await asyncio.gather(self._receiver_task, return_exceptions=True)
            self._receiver_task = None
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None
        self._prepared = False
        self._reset_greeting_state()
        await self.audio.stop()

    async def stop(self) -> None:
        was_running = self.websocket is not None
        if was_running:
            logger.info("GPT-Liveセッションを終了します")
        if self._sender_task is not None:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
            self._sender_task = None

        if self.websocket is not None:
            try:
                await self._send_event({"type": "session.close"})
                if self._receiver_task is None:
                    await asyncio.wait_for(self._wait_until_closed(), timeout=3)
                else:
                    await asyncio.wait_for(self._closed_event.wait(), timeout=3)
            except Exception:
                pass

        if self._receiver_task is not None:
            self._receiver_task.cancel()
            await asyncio.gather(self._receiver_task, return_exceptions=True)
            self._receiver_task = None

        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None

        self._prepared = False
        self._reset_greeting_state()
        await self.audio.stop()
        if was_running:
            logger.info("GPT-Liveセッションを終了しました")
        if self.archive is not None:
            self.archive.end_call("completed")
