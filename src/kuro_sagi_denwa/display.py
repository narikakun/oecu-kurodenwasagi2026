"""縦画面ブラウザへ状態と文字起こしを配信する。"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


class DisplayServer:
    def __init__(self, host: str = "0.0.0.0", http_port: int = 8080, ws_port: int = 8765) -> None:
        self.host = host
        self.http_port = http_port
        self.ws_port = ws_port
        self._clients: set[Any] = set()
        self._events: list[dict[str, Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._ws_server: Any = None
        self._kiosk_process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        directory = Path(__file__).parent / "assets" / "display"
        handler = partial(_QuietHandler, directory=str(directory))
        self._http_server = ThreadingHTTPServer((self.host, self.http_port), handler)
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name="display-http",
            daemon=True,
        )
        self._http_thread.start()
        self._ws_server = await websockets.serve(self._handle_client, self.host, self.ws_port)
        logger.info("表示画面: http://localhost:%d", self.http_port)

    async def start_kiosk(self, browser: str = "") -> None:
        executable = browser or shutil.which("chromium") or shutil.which("chromium-browser")
        if not executable:
            logger.warning(
                "Chromiumが見つからないためキオスク表示を開始できません。"
                "DISPLAY_KIOSK_BROWSERで実行ファイルを指定できます"
            )
            return

        url = f"http://localhost:{self.http_port}"
        try:
            self._kiosk_process = await asyncio.create_subprocess_exec(
                executable,
                "--kiosk",
                "--noerrdialogs",
                "--disable-infobars",
                "--disable-session-crashed-bubble",
                url,
            )
            logger.info("Chromiumキオスク表示を開始しました: %s", url)
        except OSError as error:
            logger.warning("Chromiumキオスク表示を開始できませんでした: %s", error)

    async def _handle_client(self, websocket: Any) -> None:
        self._clients.add(websocket)
        try:
            await websocket.send(
                json.dumps({"type": "snapshot", "events": self._events}, ensure_ascii=False)
            )
            await websocket.wait_closed()
        finally:
            self._clients.discard(websocket)

    def publish(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        self._events = self._events[-200:]
        if self._loop is not None and self._clients:
            self._loop.create_task(self._broadcast(event))

    def reset(self) -> None:
        self._events.clear()
        self.publish({"type": "reset"})

    async def _broadcast(self, event: dict[str, Any]) -> None:
        message = json.dumps(event, ensure_ascii=False)
        clients = list(self._clients)
        if clients:
            await asyncio.gather(
                *(client.send(message) for client in clients),
                return_exceptions=True,
            )

    async def stop(self) -> None:
        if self._kiosk_process is not None and self._kiosk_process.returncode is None:
            self._kiosk_process.terminate()
            try:
                await asyncio.wait_for(self._kiosk_process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._kiosk_process.kill()
                await self._kiosk_process.wait()
        self._kiosk_process = None
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        self._clients.clear()
