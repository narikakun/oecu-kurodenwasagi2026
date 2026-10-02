"""GPT-LiveのプライマリWebSocket接続を扱う。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from .audio import AudioDevice
from .config import Settings

logger = logging.getLogger(__name__)


class GPTLiveSession:
    URL = "wss://api.openai.com/v1/live/sessions"

    def __init__(self, settings: Settings, audio: AudioDevice) -> None:
        self.settings = settings
        self.audio = audio
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

        import websockets

        logger.info("GPT-Liveへ接続します")
        self._closed_event.clear()
        self.websocket = await websockets.connect(
            self.URL,
            additional_headers={
                "Authorization": f"Bearer {self.settings.api_key}",
            },
            max_size=None,
        )
        try:
            await self.websocket.send(json.dumps(self._session_start_event()))
            await self._wait_until_started()
            await self.audio.start()
        except Exception:
            await self.websocket.close()
            self.websocket = None
            raise

        self._sender_task = asyncio.create_task(self._send_audio_loop())
        self._receiver_task = asyncio.create_task(self._receive_loop())
        logger.info("GPT-Liveの会話を開始しました")

    def _session_start_event(self) -> dict[str, Any]:
        return {
            "type": "session.start",
            "event_id": "black_phone_start",
            "session": {
                "model": self.settings.live_model,
                "instructions": self.settings.live_instructions,
                "audio": {
                    "format": {
                        "type": "audio/pcm",
                        "rate": self.settings.sample_rate,
                    },
                    "output": {"voice": self.settings.live_voice},
                },
                # 初期版では外部ツールを使わず、音声会話だけに絞る。
                "delegation": {"type": "client"},
                "store": False,
            },
        }

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
                    await self.audio.play(base64.b64decode(event["delta"]))
                elif event_type == "session.closed":
                    self._closed_event.set()
                    return
                elif event_type == "error":
                    message = event.get("error", {}).get("message", "不明なエラー")
                    logger.error("GPT-Liveエラー: %s", message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("GPT-Liveの受信処理が終了しました")
        finally:
            self._closed_event.set()

    async def stop(self) -> None:
        if not self.is_running:
            return

        assert self.websocket is not None
        if self._sender_task is not None:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)

        await self.audio.stop()

        try:
            await self.websocket.send(json.dumps({"type": "session.close"}))
            await asyncio.wait_for(self._closed_event.wait(), timeout=3)
        except Exception as error:
            # 切断時の通信エラーは終了処理を妨げない。
            logger.debug("GPT-Liveの終了応答を待たずに切断します: %s", error)
        finally:
            if self._receiver_task is not None:
                self._receiver_task.cancel()
                await asyncio.gather(self._receiver_task, return_exceptions=True)
            await self.websocket.close()
            self.websocket = None
            self._sender_task = None
            self._receiver_task = None
            logger.info("GPT-Liveの会話を終了しました")
