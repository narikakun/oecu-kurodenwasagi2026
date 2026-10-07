"""GPT-Liveとの最小限の双方向音声セッションを扱う。"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from .audio import AudioDevice
from .config import Settings


class GPTLiveSession:
    """1接続だけを使い、受話器とGPT-Liveの音声をそのまま中継する。"""

    URL = "wss://api.openai.com/v1/live/sessions"
    DEFAULT_INSTRUCTIONS = "日本語で自然に会話してください。"

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

        try:
            await self.audio.start()
            self._closed_event.clear()
            self.websocket = await websockets.connect(
                self.URL,
                additional_headers={"Authorization": f"Bearer {self.settings.api_key}"},
                max_size=None,
            )
            await self.websocket.send(json.dumps(self._session_start_event()))
            await self._wait_until_started()

            # GPT-Liveは全二重なので、開始後は入力を止めず常時送る。
            self._receiver_task = asyncio.create_task(self._receive_loop())
            self._sender_task = asyncio.create_task(self._send_audio_loop())
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
            },
        }

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
                await self.websocket.send(
                    json.dumps(
                        {
                            "type": "session.input_audio.append",
                            "audio": base64.b64encode(pcm).decode("ascii"),
                        }
                    )
                )
            finally:
                self.audio.input_queue.task_done()

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
        except asyncio.CancelledError:
            raise
        finally:
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
                await self.websocket.send(json.dumps({"type": "session.close"}))
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
