"""黒電話の操作とGPT-Liveセッションを結び付ける。"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Protocol

from .hardware import HardwareEventType

logger = logging.getLogger(__name__)


class Session(Protocol):
    @property
    def is_running(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class AppState(Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONVERSATION = "conversation"
    ENDING = "ending"
    ERROR = "error"


class PhoneController:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.state = AppState.IDLE
        self.last_digit: int | None = None

    async def handle(self, event) -> None:
        if event.type == HardwareEventType.HOOK_UP:
            await self._start_conversation()
        elif event.type == HardwareEventType.HOOK_DOWN:
            await self._end_conversation()
        elif event.type == HardwareEventType.DIAL:
            self.last_digit = event.digit
            logger.info("ダイヤル入力: %s", event.digit)

    async def _start_conversation(self) -> None:
        if self.session.is_running or self.state == AppState.CONNECTING:
            return
        self.state = AppState.CONNECTING
        try:
            await self.session.start()
            self.state = AppState.CONVERSATION
        except Exception:
            self.state = AppState.ERROR
            logger.exception("会話を開始できませんでした")

    async def _end_conversation(self) -> None:
        if not self.session.is_running:
            self.state = AppState.IDLE
            return
        self.state = AppState.ENDING
        try:
            await self.session.stop()
        finally:
            self.state = AppState.IDLE

