"""確定した会話記録を外部のPHP APIへ非同期送信する。"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """認証トークンを別URLへ転送しない。"""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TranscriptArchive:
    """通話を止めずに会話イベントを順番どおりアップロードする。"""

    MAX_QUEUE_SIZE = 500
    RETRY_DELAYS = (1, 2, 4, 8, 16)

    def __init__(self, api_url: str, api_token: str, device_name: str) -> None:
        self.api_url = api_url.strip()
        self.api_token = api_token.strip()
        self.device_name = device_name.strip() or "black-phone"
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=self.MAX_QUEUE_SIZE
        )
        self._worker: asyncio.Task[None] | None = None
        self._call_id: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_url and self.api_token)

    @property
    def current_call_id(self) -> str | None:
        return self._call_id

    def start(self) -> None:
        if self.enabled and (self._worker is None or self._worker.done()):
            self._worker = asyncio.create_task(
                self._run(), name="transcript-archive"
            )
            logger.info("会話記録のリアルタイム送信を有効にしました: %s", self.api_url)

    def begin_call(self) -> str | None:
        if not self.enabled:
            return None
        if self._call_id is not None:
            return self._call_id
        self._call_id = str(uuid.uuid4())
        self._enqueue(
            {
                "action": "call_started",
                "event_id": str(uuid.uuid4()),
                "call_id": self._call_id,
                "device_name": self.device_name,
                "occurred_at": _now_iso(),
            }
        )
        return self._call_id

    def record_transcript(self, speaker: str, text: str) -> None:
        if self._call_id is None or not text:
            return
        self._enqueue(
            {
                "action": "transcript",
                "event_id": str(uuid.uuid4()),
                "call_id": self._call_id,
                "speaker": speaker,
                "text": text,
                "occurred_at": _now_iso(),
            }
        )

    def end_call(self, status: str = "completed") -> None:
        if self._call_id is None:
            return
        self._enqueue(
            {
                "action": "call_ended",
                "event_id": str(uuid.uuid4()),
                "call_id": self._call_id,
                "status": status,
                "occurred_at": _now_iso(),
            }
        )
        self._call_id = None

    def _enqueue(self, payload: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.error("会話記録の送信待ちが上限に達したため、記録を1件破棄しました")

    async def _run(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self._send_with_retry(payload)
            finally:
                self._queue.task_done()

    async def _send_with_retry(self, payload: dict[str, Any]) -> None:
        attempts = len(self.RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                await asyncio.to_thread(self._post, payload)
                return
            except Exception as error:
                if attempt == attempts - 1:
                    logger.error(
                        "会話記録を送信できませんでした（event_id=%s）: %s",
                        payload.get("event_id", ""),
                        error,
                    )
                    return
                delay = self.RETRY_DELAYS[attempt]
                logger.warning(
                    "会話記録の送信に失敗しました。%d秒後に再試行します: %s",
                    delay,
                    error,
                )
                await asyncio.sleep(delay)

    def _post(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.api_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "kuro-sagi-denwa/0.1",
            },
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(request, timeout=10) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f"HTTP {response.status}")
                raw_response = response.read(4096)
                result = json.loads(raw_response.decode("utf-8"))
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise RuntimeError("サーバーから正常応答が返りませんでした")
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"HTTP {error.code}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("サーバー応答がJSONではありません") from error

    async def stop(self) -> None:
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("未送信の会話記録を残したまま送信処理を終了します")
        self._worker.cancel()
        await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
