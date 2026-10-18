"""GPT-Liveとの最小限の双方向音声セッションを扱う。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import wave
from pathlib import Path
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
# 終了句の判定前に取り除く空白と句読点。文字起こしの区切り方の揺れを吸収する。
_FEEDBACK_TRIGGER_IGNORED_RE = re.compile(r"[\s、。,.，．!！?？…・「」『』]")
# 「それでは失礼します」「では失礼します」「それでは失礼いたします」などを受け付ける。
# 冒頭の「突然のお電話で失礼します」に反応しないよう、前置きの「では」等は必須にする。
_FEEDBACK_TRIGGER_RE = re.compile(
    r"(?:それでは|それじゃあ?|では|じゃあ?)(?:失礼|しつれい)(?:いた|致)?します"
)


def is_feedback_trigger(text: str) -> bool:
    """犯人役の終了句が含まれているかを、表記や区切りの揺れを許して判定する。"""
    return _FEEDBACK_TRIGGER_RE.search(
        _FEEDBACK_TRIGGER_IGNORED_RE.sub("", text)
    ) is not None
_TRANSCRIPT_TERM_REPLACEMENTS = {
    "エーティーエム": "ATM",
    "エー・ティー・エム": "ATM",
    "えーてぃーえむ": "ATM",
    "ＡＴＭ": "ATM",
    "シャープ": "#",
    "しゃーぷ": "#",
    "＃": "#",
}


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
    for spoken, written in _TRANSCRIPT_TERM_REPLACEMENTS.items():
        normalized = normalized.replace(spoken, written)
    return normalize_transcript_numbers(normalized)


class GPTLiveSession:
    """1接続だけを使い、受話器とGPT-Liveの音声をそのまま中継する。"""

    URL = "wss://api.openai.com/v1/live/sessions"
    GREETING_AUDIO_PATH = (
        Path(__file__).parent / "assets" / "audio" / "moshimoshi-meridian.wav"
    )
    DISCONNECT_AUDIO_PATH = (
        Path(__file__).parent / "assets" / "audio" / "call-disconnect.wav"
    )
    FEEDBACK_TRIGGER_TEXT = "それでは失礼します"
    FEEDBACK_RESUME_INSTRUCTION = (
        "通話終了音の再生が完了しました。犯人役は完全に終了してください。"
        "このセッションで実際に交わした会話だけを根拠に、今すぐ中立の振り返りを始めてください。"
        "相手が実際に答えた内容を具体的に取り上げ、良かった判断と注意すべき判断、"
        "会話中に現れた詐欺の兆候を短く説明してください。"
        "会話になかった発言や行動は作らないでください。"
    )
    DEFAULT_INSTRUCTIONS = (
        "実際の電話のように、日本語で自然に会話してください。"
        "一度の発話は原則1〜2文にし、一度に複数の質問をせず、質問は1つだけにしてください。"
        "短い相づちや感想も使い、毎回質問で返さないでください。"
        "長い説明が必要な場合は短く区切り、相手の反応を待ってください。"
        "Interruption policy: 受話器越しの相手があなたに話しかけたときだけ、"
        "発話を止めて最後まで聞いてください。"
        "近くにいる人同士の会話など、あなたへ向けられていない音声は割り込みとして扱わず、"
        "電話での話を続けてください。"
        "相手からあなたへの発話が一区切りしたら、短く応答して会話を進めてください。"
        "通常の挨拶や簡単な質問にはすぐ答えてください。"
        "発音ルール: 『守口』の漢字をそのまま読まず、発話するときは毎回必ず"
        "『もりぐち』というひらがなの音へ置き換えてください。"
        "『まもりくち』という読み方は禁止です。『守口市役所』は必ず"
        "『もりぐちしやくしょ』と発音してください。"
        "『保険年金課』を『ほけんねんきんか』、『還付金』を『かんぷきん』、"
        "『医療費』を『いりょうひ』、『ATM』を『エーティーエム』と読んでください。"
        "『22,560円』は『にまんにせんごひゃくろくじゅうえん』と読んでください。"
    )
    CONTROL_ACK_TIMEOUT_SECONDS = 15
    FIRST_AUDIO_TIMEOUT_SECONDS = 1
    TRANSCRIPT_IDLE_SECONDS = 1.0
    TRANSCRIPT_SENTENCE_SECONDS = 0.15
    # GPT-Liveの主WebSocketには出力音声の完了イベントがないため、字幕が先行しやすい
    # Raspberry Piでも末尾音声が届くまで待てる長さを確保する。
    OUTPUT_AUDIO_QUIET_SECONDS = 0.5
    OUTPUT_AUDIO_FINISH_TIMEOUT_SECONDS = 2.0
    DISCONNECT_AUDIO_ENQUEUE_TIMEOUT_SECONDS = 12.0
    FEEDBACK_QUIET_SECONDS = 5.0
    FEEDBACK_FINISH_TIMEOUT_SECONDS = 45.0
    INPUT_BACKLOG_RESTART_SECONDS = 10
    RATE_LIMIT_RESTART_COUNT = 3
    RATE_LIMIT_RESTART_WINDOW_SECONDS = 30

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
        self._feedback_transition_task: asyncio.Task[None] | None = None
        self._feedback_transition_started = False
        self._feedback_instruction_sent = False
        self._feedback_done_event = asyncio.Event()
        self._feedback_completion_task: asyncio.Task[None] | None = None
        self._feedback_started_at = 0.0
        self._handset_down = False
        self._prepare_lock = asyncio.Lock()
        self._closed_event = asyncio.Event()
        self._prepared = False
        self._next_audio_send_time = 0.0
        self._last_output_audio_at = 0.0
        self._last_output_audio_log_at = 0.0
        self._output_audio_burst_count = 0
        self._rate_limit_count = 0
        self._last_rate_limit_at = 0.0
        self._failure_future: asyncio.Future[BaseException] | None = None
        self._stopping = False
        self._background_failed = False
        self._transcript_buffers: dict[str, str] = {}
        self._transcript_flush_handles: dict[str, asyncio.TimerHandle] = {}

    @property
    def is_running(self) -> bool:
        return self.websocket is not None

    @property
    def is_conversation_active(self) -> bool:
        return any(
            task is not None and not task.done()
            for task in (self._sender_task, self._receiver_task)
        )

    @property
    def is_feedback_active(self) -> bool:
        return self._feedback_transition_started and not self._feedback_done_event.is_set()

    async def wait_for_failure(self) -> None:
        """バックグラウンド送受信の異常をメイン監視ループへ伝える。"""
        if self._failure_future is None:
            self._failure_future = asyncio.get_running_loop().create_future()
        error = await self._failure_future
        raise RuntimeError("GPT-Liveのバックグラウンド処理が停止しました") from error

    async def prepare(self) -> None:
        """呼び出し前にGPT-Liveへの接続だけを準備する。"""
        async with self._prepare_lock:
            await self._prepare_locked()

    async def _prepare_locked(self) -> None:
        if self._prepared and self.websocket is not None:
            return
        import websockets

        try:
            logger.info("GPT-Liveセッションへ接続します")
            self._feedback_transition_started = False
            self._feedback_instruction_sent = False
            self._feedback_done_event.clear()
            self._feedback_started_at = 0.0
            self._handset_down = False
            self._closed_event.clear()
            self._next_audio_send_time = 0.0
            self.websocket = await websockets.connect(
                self.URL,
                additional_headers={"Authorization": f"Bearer {self.settings.api_key}"},
                max_size=None,
            )
            await self._send_event(self._session_start_event())
            await self._wait_until_started()
            self._prepared = True
            logger.info("GPT-Liveセッションを開始しました（音声入力開始前）")
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

        self._stopping = False
        self._background_failed = False
        self._handset_down = False
        if self.archive is not None:
            self.archive.begin_call()
        try:
            await self.prepare()
            await self.audio.start()

            # 録音済みの挨拶を確実に再生してから、マイク音声の送信を始める。
            self.audio.clear_input_queue()
            await self._play_recorded_greeting()
            self.audio.clear_input_queue()
            await self._send_first_audio_packet()
            self._sender_task = asyncio.create_task(self._send_audio_loop())
            self._sender_task.add_done_callback(self._report_sender_failure)
            self._receiver_task = asyncio.create_task(self._receive_loop())
            self._receiver_task.add_done_callback(self._report_receiver_failure)
            logger.info("GPT-Liveの通話を開始しました")
        except asyncio.CancelledError:
            # 開始中に受話器を置かれた場合も、送信タスクや接続を残さない。
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

    @classmethod
    def _load_recorded_greeting(cls) -> bytes:
        with wave.open(str(cls.GREETING_AUDIO_PATH), "rb") as greeting:
            if (
                greeting.getnchannels() != 1
                or greeting.getsampwidth() != 2
                or greeting.getframerate() != 24_000
            ):
                raise RuntimeError(
                    "録音済みの「もしもし？」は24kHz・モノラル・PCM16である必要があります"
                )
            return greeting.readframes(greeting.getnframes())

    async def _play_recorded_greeting(self) -> None:
        pcm = await asyncio.to_thread(self._load_recorded_greeting)
        await self.audio.play(pcm)
        await self.audio.wait_until_played()
        greeting_text = "もしもし？"
        transcript_logger.info("AI: %s", greeting_text)
        if self.archive is not None:
            self.archive.record_transcript("assistant", greeting_text)
        if self.display is not None:
            self.display.publish(
                {
                    "type": "transcript",
                    "speaker": "assistant",
                    "status": "final",
                    "text": greeting_text,
                }
            )
        logger.info("録音済みの最初の「もしもし？」を再生しました")

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
        logger.info(
            "同期計測 transcript_received speaker=%s monotonic_ms=%d start_ms=%s end_ms=%s chars=%d",
            "user" if label == "参加者" else "assistant",
            round(time.monotonic() * 1000),
            event.get("start_ms"),
            event.get("end_ms"),
            len(delta),
        )
        if (
            assistant
            and not self._feedback_transition_started
            and is_feedback_trigger(self._transcript_buffers[label])
        ):
            logger.info("終了句を検出しました: %s", self._transcript_buffers[label])
            self._feedback_transition_started = True
            self._feedback_transition_task = asyncio.create_task(
                self._play_disconnect_and_resume_feedback()
            )
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

    async def _play_disconnect_and_resume_feedback(self) -> None:
        """犯人役の終了後に切断音を挟み、GPTへ振り返り再開を伝える。"""
        input_muted = False
        transition_failed = False
        try:
            logger.info("終了句を検出したため末尾音声の再生完了を待ちます")
            # Live WebSocketには発話完了イベントがないため、終了句を検出した後は
            # 入力を止め、音声デルタの受信と実機の再生が静まるまで待つ。
            await self._send_event(
                {
                    "type": "session.input_audio.mute",
                    "event_id": "mute_during_disconnect_sound",
                }
            )
            input_muted = True
            await self._wait_for_output_audio_to_finish()
            logger.info("詐欺電話終了の効果音を再生します")
            try:
                await asyncio.wait_for(
                    self.audio.enqueue_audio_file(self.DISCONNECT_AUDIO_PATH),
                    timeout=self.DISCONNECT_AUDIO_ENQUEUE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "切断音を時間内に再生キューへ追加できませんでした"
                )
            # ここでは物理再生の完了を待たない。切断音を全量投入してからGPTへ
            # 指示することで、後から届くフィードバック音声が必ずその後ろへ並ぶ。
            self.audio.clear_input_queue()
            if self.display is not None:
                self.display.publish(
                    {
                        "type": "feedback",
                        "active": True,
                        "image": "refund-fraud-warning.png",
                        "alt": "還付金詐欺にご用心！",
                    }
                )
            await self._send_event(
                {
                    "type": "session.instructions.append",
                    "event_id": "resume_feedback_after_disconnect",
                    "delegation_id": None,
                    "content": self.FEEDBACK_RESUME_INSTRUCTION,
                }
            )
            self._feedback_instruction_sent = True
            self._feedback_started_at = asyncio.get_running_loop().time()
            self._feedback_completion_task = asyncio.create_task(
                self._finish_feedback_if_no_output()
            )
            logger.info("GPT-Liveへ振り返りの開始を指示しました")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("詐欺電話終了の効果音を再生できませんでした")
            transition_failed = True
        finally:
            if input_muted and self.websocket is not None and not self._feedback_instruction_sent:
                try:
                    await self._send_event(
                        {
                            "type": "session.input_audio.unmute",
                            "event_id": "recover_input_after_disconnect_error",
                        }
                    )
                except Exception:
                    logger.exception("終話音エラー後に音声入力を再開できませんでした")
            if transition_failed:
                self._mark_feedback_done()

    async def continue_feedback_after_hangup(self) -> None:
        """受話器を置いた後も出力受信だけを残して振り返りを継続する。"""
        self._handset_down = True
        if self._sender_task is not None:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
            self._sender_task = None
        self.audio.clear_input_queue()
        if self.websocket is not None:
            await self._send_event(
                {
                    "type": "session.input_audio.mute",
                    "event_id": "mute_after_feedback_hangup",
                }
            )

    async def wait_for_feedback_completion(self) -> None:
        await self._feedback_done_event.wait()

    def _mark_feedback_done(self) -> None:
        """成功・失敗にかかわらずフィードバック待ちを確実に解除する。"""
        if self._feedback_done_event.is_set():
            return
        self._feedback_done_event.set()
        if self.display is not None:
            self.display.publish({"type": "feedback", "active": False})

    def _note_feedback_output(self) -> None:
        if not self._feedback_instruction_sent or self._feedback_done_event.is_set():
            return
        if self._feedback_completion_task is not None:
            self._feedback_completion_task.cancel()
        self._feedback_completion_task = asyncio.create_task(
            self._complete_feedback_after_quiet()
        )

    async def _complete_feedback_after_quiet(self) -> None:
        try:
            await asyncio.sleep(self.FEEDBACK_QUIET_SECONDS)
            elapsed = asyncio.get_running_loop().time() - self._feedback_started_at
            remaining = max(0.1, self.FEEDBACK_FINISH_TIMEOUT_SECONDS - elapsed)
            await asyncio.wait_for(self.audio.wait_until_played(), timeout=remaining)
            self._mark_feedback_done()
            logger.info("フィードバック音声の再生完了を検出しました")
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("フィードバック音声の完了待ちがタイムアウトしました")
            self._mark_feedback_done()

    async def _finish_feedback_if_no_output(self) -> None:
        try:
            await asyncio.sleep(self.FEEDBACK_FINISH_TIMEOUT_SECONDS)
            logger.warning("フィードバック出力が完了時間内に確認できませんでした")
            self._mark_feedback_done()
        except asyncio.CancelledError:
            raise

    async def _wait_for_output_audio_to_finish(self) -> None:
        """音声デルタが静まり、端末の再生が完了した状態を確認する。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.OUTPUT_AUDIO_FINISH_TIMEOUT_SECONDS
        # 終了字幕より音声が先に届き終わる場合もあるため、新しい音声デルタの到着は
        # 必須にせず、既存キューの再生完了と静音時間で判定する。
        self._last_output_audio_at = max(self._last_output_audio_at, loop.time())
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "末尾音声の完了を%.1f秒待ちましたが、切断処理を続行します",
                    self.OUTPUT_AUDIO_FINISH_TIMEOUT_SECONDS,
                )
                return
            received_at = self._last_output_audio_at
            quiet_remaining = self.OUTPUT_AUDIO_QUIET_SECONDS - (
                loop.time() - received_at
            )
            if quiet_remaining > 0:
                await asyncio.sleep(min(quiet_remaining, remaining))
            remaining = deadline - loop.time()
            if remaining <= 0:
                continue
            try:
                await asyncio.wait_for(
                    self.audio.wait_until_played(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                continue
            if (
                self._last_output_audio_at == received_at
                and loop.time() - received_at >= self.OUTPUT_AUDIO_QUIET_SECONDS
            ):
                return

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

    def _report_receiver_failure(self, task: asyncio.Task[None]) -> None:
        self._report_task_failure(task, "GPT-Liveの受信処理が停止しました")

    def _report_sender_failure(self, task: asyncio.Task[None]) -> None:
        self._report_task_failure(task, "GPT-Liveの音声送信が停止しました")

    def _report_task_failure(self, task: asyncio.Task[None], message: str) -> None:
        if task.cancelled() or self._stopping:
            return
        error = task.exception()
        if error is None:
            error = RuntimeError(f"{message}（予期せず終了）")
            logger.error("%s（予期せず終了）", message)
        else:
            logger.error(
                message,
                exc_info=(type(error), error, error.__traceback__),
            )
        if self._failure_future is None:
            self._failure_future = asyncio.get_running_loop().create_future()
        if not self._failure_future.done():
            self._background_failed = True
            self._failure_future.set_result(error)

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
        # キューに溜まった音声を一気に送らず、PCM16モノラルの実時間で送る。
        # 遅延した時間を送信枠として貯めない（停止後の高速な追いつきを防ぐ）。
        loop = asyncio.get_running_loop()
        while self._next_audio_send_time > loop.time():
            await asyncio.sleep(self._next_audio_send_time - loop.time())
        duration = len(pcm) / (self.settings.sample_rate * 2)
        started = loop.time()
        scheduled = self._next_audio_send_time
        if started - scheduled > duration:
            scheduled = started
        # 送信開始時刻で予約し、送信処理時間を次の待ち時間に上乗せしない。
        # 小さな待機誤差は吸収するが、追いつく速度は実時間を超えない。
        self._next_audio_send_time = scheduled + duration
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
                restart_threshold = max(
                    1,
                    self.INPUT_BACKLOG_RESTART_SECONDS
                    * 1_000
                    // self.settings.block_ms,
                )
                if self.audio.input_drop_streak >= restart_threshold:
                    raise RuntimeError(
                        "入力音声の滞留が継続したためGPT-Liveを再起動します"
                        f"（連続破棄{self.audio.input_drop_streak}ブロック）"
                    )
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
        self._log_transcript(event)

        if event_type == "session.output_audio.delta":
            self._note_feedback_output()
            received_at = asyncio.get_running_loop().time()
            pcm = base64.b64decode(event["delta"])
            if received_at - self._last_output_audio_log_at >= 0.25:
                self._output_audio_burst_count += 1
                logger.info(
                    "同期計測 audio_received burst=%d monotonic_ms=%d bytes=%d",
                    self._output_audio_burst_count,
                    round(received_at * 1000),
                    len(pcm),
                )
            self._last_output_audio_log_at = received_at
            self._last_output_audio_at = received_at
            await self.audio.play(pcm)
        elif event_type == "error":
            error = event.get("error", {})
            message = error.get("message", "不明なエラー")
            code = error.get("code")
            if code == "input_audio_rate_limit_exceeded":
                now = asyncio.get_running_loop().time()
                if (
                    self._last_rate_limit_at
                    and now - self._last_rate_limit_at
                    > self.RATE_LIMIT_RESTART_WINDOW_SECONDS
                ):
                    self._rate_limit_count = 0
                self._last_rate_limit_at = now
                self._rate_limit_count += 1
                if self._rate_limit_count == 1 or self._rate_limit_count % 10 == 0:
                    logger.warning(
                        "入力音声の送信速度制限を検出しました（累計%d回）: %s",
                        self._rate_limit_count,
                        message,
                    )
                self.audio.clear_input_queue()
                self._next_audio_send_time = max(
                    self._next_audio_send_time,
                    now + 1.0,
                )
                if self._rate_limit_count >= self.RATE_LIMIT_RESTART_COUNT:
                    raise RuntimeError(
                        "入力音声の送信速度制限が継続したためGPT-Liveを再起動します"
                    )
                return False
            if code:
                raise RuntimeError(f"GPT-Liveエラー [{code}]: {message}")
            raise RuntimeError(f"GPT-Liveエラー: {message}")
        elif event_type == "session.closed":
            self._closed_event.set()
            return True
        return False

    async def _abort_start(self) -> None:
        self._stopping = True
        if self._feedback_completion_task is not None:
            self._feedback_completion_task.cancel()
            await asyncio.gather(
                self._feedback_completion_task, return_exceptions=True
            )
            self._feedback_completion_task = None
        if self._feedback_transition_task is not None:
            self._feedback_transition_task.cancel()
            await asyncio.gather(
                self._feedback_transition_task, return_exceptions=True
            )
            self._feedback_transition_task = None
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
        await self.audio.stop()
        self._stopping = False

    async def stop(self) -> None:
        self._stopping = True
        was_running = self.websocket is not None
        if was_running:
            logger.info("GPT-Liveセッションを終了します")
        if self._feedback_completion_task is not None:
            self._feedback_completion_task.cancel()
            await asyncio.gather(
                self._feedback_completion_task, return_exceptions=True
            )
            self._feedback_completion_task = None
        if self._feedback_transition_task is not None:
            self._feedback_transition_task.cancel()
            await asyncio.gather(
                self._feedback_transition_task, return_exceptions=True
            )
            self._feedback_transition_task = None
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
        await self.audio.stop()
        if was_running:
            logger.info("GPT-Liveセッションを終了しました")
        if self.archive is not None:
            self.archive.end_call("error" if self._background_failed else "completed")
        self._stopping = False
