"""GPT-Liveとの最小限の双方向音声セッションを扱う。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from .audio import AudioDevice
from .config import Settings

transcript_logger = logging.getLogger("kuro_sagi_denwa.transcript")
logger = logging.getLogger(__name__)


class GPTLiveSession:
    """1接続だけを使い、受話器とGPT-Liveの音声をそのまま中継する。"""

    URL = "wss://api.openai.com/v1/live/sessions"
    DEFAULT_INSTRUCTIONS = (
        "日本語で自然に会話してください。通常の挨拶や簡単な質問にはすぐ答えてください。"
        "詳しい確認が必要な質問はResponsesバックエンドへ委譲し、結果が届いたら必ず会話を"
        "再開して回答してください。結果を待たないまま会話を終わらせないでください。"
    )
    GREETING_INSTRUCTION = (
        "今すぐ日本語で「もしもし？」とだけ話してください。"
        "その後は何も続けず、相手の返答を待ってください。"
    )
    TRANSCRIPT_IDLE_SECONDS = 1.0
    TRANSCRIPT_SENTENCE_SECONDS = 0.15

    def __init__(self, settings: Settings, audio: AudioDevice, display: Any = None) -> None:
        self.settings = settings
        self.audio = audio
        self.display = display
        self.websocket: Any = None
        self._sender_task: asyncio.Task[None] | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._closed_event = asyncio.Event()
        self._transcript_buffers: dict[str, str] = {}
        self._transcript_flush_handles: dict[str, asyncio.TimerHandle] = {}

    @property
    def is_running(self) -> bool:
        return self.websocket is not None

    async def start(self) -> None:
        if self.is_running:
            return

        import websockets

        try:
            await self.audio.start()
            self._closed_event.clear()
            self.websocket = await websockets.connect(
                self.URL,
                additional_headers={"Authorization": f"Bearer {self.settings.api_key}"},
                max_size=None,
            )
            await self._send_event(self._session_start_event())
            await self._wait_until_started()

            # GPT-Liveは全二重なので、開始後は入力を止めず常時送る。
            self._receiver_task = asyncio.create_task(self._receive_loop())
            self._receiver_task.add_done_callback(self._report_receiver_failure)
            self._sender_task = asyncio.create_task(self._send_audio_loop())
            self._sender_task.add_done_callback(self._report_sender_failure)
            await self._send_event(self._greeting_event())
        except Exception:
            await self._abort_start()
            raise

    def _session_start_event(self) -> dict[str, Any]:
        instructions = self.DEFAULT_INSTRUCTIONS
        if self.settings.live_instructions:
            instructions += "\n\n" + self.settings.live_instructions

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
                    "responses": {
                        "model": self.settings.live_backend_model,
                        "instructions": (
                            "ユーザーの質問に日本語で正確かつ簡潔に答えてください。"
                            "最新情報を確認できない場合は、その点を明示してください。"
                        ),
                    },
                },
            },
        }

    def _greeting_event(self) -> dict[str, Any]:
        return {
            "type": "session.instructions.append",
            "event_id": "initial_greeting",
            "delegation_id": None,
            "content": self.GREETING_INSTRUCTION,
        }

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
        if not current:
            transcript_logger.info("文字起こし中（%s）: %s", label, delta)
        self._transcript_buffers[label] = current + delta
        if self.display is not None:
            self.display.publish(
                {
                    "type": "transcript",
                    "speaker": "user" if label == "参加者" else "assistant",
                    "status": "partial",
                    "text": self._transcript_buffers[label],
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
            transcript_logger.info("%s: %s", label, text)
            if self.display is not None:
                self.display.publish(
                    {
                        "type": "transcript",
                        "speaker": "user" if label == "参加者" else "assistant",
                        "status": "final",
                        "text": text,
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
            try:
                await self._send_event(
                    {
                        "type": "session.input_audio.append",
                        "audio": base64.b64encode(pcm).decode("ascii"),
                    }
                )
            finally:
                self.audio.input_queue.task_done()

    async def _receive_loop(self) -> None:
        assert self.websocket is not None
        try:
            async for raw in self.websocket:
                event = json.loads(raw)
                event_type = event.get("type")

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
                    return
        except asyncio.CancelledError:
            raise
        finally:
            self._flush_all_transcripts()
            self._closed_event.set()

    async def _abort_start(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None
        await self.audio.stop()

    async def stop(self) -> None:
        if self._sender_task is not None:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
            self._sender_task = None

        if self.websocket is not None:
            try:
                await self._send_event({"type": "session.close"})
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

        await self.audio.stop()
