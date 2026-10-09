"""GPT-Liveとの最小限の双方向音声セッションを扱う。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from .audio import AudioDevice
from .config import Settings
from .scenario import build_refund_fraud_scenario

transcript_logger = logging.getLogger("kuro_sagi_denwa.transcript")
logger = logging.getLogger(__name__)


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
    )
    GREETING_COMMENTARY = "もしもし？"
    GREETING_TIMEOUT_SECONDS = 5
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
        self._prepared = False
        self._transcript_buffers: dict[str, str] = {}
        self._transcript_flush_handles: dict[str, asyncio.TimerHandle] = {}

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
            self._prepared = True
            logger.info("GPT-Liveセッションを開始しました（入力ミュート中）")
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

        try:
            await self.prepare()
            await self.audio.start()

            # GPT-Liveのタイムラインを進めるため、入力音声の送信を開始してから
            # 挨拶指示を送り、挨拶の処理中も入力ストリームを継続する。
            self.audio.clear_input_queue()
            self._sender_task = asyncio.create_task(self._send_audio_loop())
            self._sender_task.add_done_callback(self._report_sender_failure)
            await self._send_event(
                {"type": "session.input_audio.unmute", "event_id": "handset_up_unmute"}
            )
            await self._wait_for_ack(
                "session.input_audio.unmuted", "handset_up_unmute"
            )
            await self._send_event(self._greeting_event())
            await self._wait_until_greeting_accepted()

            # 挨拶後は通常の受信ループへ引き継ぐ。
            self._receiver_task = asyncio.create_task(self._receive_loop())
            self._receiver_task.add_done_callback(self._report_receiver_failure)
            logger.info("GPT-Liveの通話を開始しました")
        except (Exception, asyncio.CancelledError):
            # 挨拶中に受話器を置かれた場合も、送信タスクや接続を残さない。
            await self._abort_start()
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
            "type": "session.commentary.append",
            "event_id": event_id,
            "delegation_id": None,
            "content": self.GREETING_COMMENTARY,
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

    async def _wait_for_ack(self, event_type: str, client_event_id: str) -> None:
        assert self.websocket is not None
        while True:
            raw = await asyncio.wait_for(self.websocket.recv(), timeout=15)
            event = json.loads(raw)
            await self._handle_server_event(event)
            if (
                event.get("type") == event_type
                and event.get("client_event_id") == client_event_id
            ):
                return

    async def _wait_until_greeting_accepted(self) -> None:
        """挨拶の受理、音声出力、文字起こしの「もしもし」を確認する。"""
        assert self.websocket is not None
        commentary_accepted = False
        audio_received = False
        greeting_transcript = ""
        greeting_event_ids = {"initial_greeting"}
        retried = False
        while not (
            commentary_accepted
            and audio_received
            and "もしもし" in greeting_transcript
        ):
            try:
                raw = await asyncio.wait_for(
                    self.websocket.recv(), timeout=self.GREETING_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                if retried:
                    raise RuntimeError(
                        "GPT-Liveから初回挨拶「もしもし？」が返りませんでした"
                    )
                retry_event_id = "initial_greeting_retry"
                greeting_event_ids.add(retry_event_id)
                logger.warning(
                    "GPT-Liveの初回挨拶が届かないため、挨拶を再送します"
                )
                await self._send_event(self._greeting_event(retry_event_id))
                retried = True
                continue
            event = json.loads(raw)
            await self._handle_server_event(event)
            if (
                event.get("type") == "session.commentary.appended"
                and event.get("client_event_id") in greeting_event_ids
            ):
                commentary_accepted = True
            elif event.get("type") == "session.output_audio.delta":
                audio_received = True
            elif event.get("type") == "session.output_transcript.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    greeting_transcript += delta
        await self.audio.wait_until_played()
        logger.info("GPT-Liveの初回挨拶を確認しました: %s", greeting_transcript)

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
        await self.audio.stop()
        if was_running:
            logger.info("GPT-Liveセッションを終了しました")
