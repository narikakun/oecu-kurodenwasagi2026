"""黒電話の操作とGPT-Liveセッションを結び付ける。"""

from __future__ import annotations

import asyncio
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

    async def notify_dial(self, digit: int) -> None: ...


class Ringer(Protocol):
    @property
    def is_ringing(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class DeviceSelector(Protocol):
    active: bool

    def enter(self) -> None: ...

    def exit(self) -> None: ...

    def handle_digit(self, digit: int) -> None: ...


class AppState(Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONVERSATION = "conversation"
    ENDING = "ending"
    ERROR = "error"


class PhoneController:
    def __init__(
        self,
        session: Session,
        ringer: Ringer | None = None,
        device_selector: DeviceSelector | None = None,
        display=None,
    ) -> None:
        self.session = session
        self.ringer = ringer
        self.device_selector = device_selector
        self.display = display
        self.state = AppState.IDLE
        self.last_digit: int | None = None
        self._hook_up = False
        self._ring_delay_task: asyncio.Task[None] | None = None

    async def handle(self, event) -> None:
        if event.type == HardwareEventType.HOOK_UP:
            self._hook_up = True
            if self.device_selector is not None:
                self.device_selector.exit()
            await self._cancel_ring()
            await self._start_conversation()
        elif event.type == HardwareEventType.HOOK_DOWN:
            self._hook_up = False
            await self._end_conversation()
            if self.display is not None:
                self.display.reset()
        elif event.type == HardwareEventType.DIAL:
            self.last_digit = event.digit
            logger.info("ダイヤル入力: %s", event.digit)
            if (
                event.digit is not None
                and not self._hook_up
                and self.device_selector is not None
                and (self.device_selector.active or self._ringer_is_ringing())
            ):
                if not self.device_selector.active:
                    await self._cancel_ring()
                    self.device_selector.enter()
                    self._display_state("settings", "音声設定中")
                self.device_selector.handle_digit(event.digit)
            elif event.digit is not None and not self._hook_up:
                self._schedule_ring(event.digit)
            elif event.digit is not None and self.session.is_running:
                await self.session.notify_dial(event.digit)

    def _ringer_is_ringing(self) -> bool:
        return self.ringer is not None and self.ringer.is_ringing

    def _schedule_ring(self, delay_seconds: int) -> None:
        if self.ringer is None:
            return
        if self._ring_delay_task is not None:
            self._ring_delay_task.cancel()
        self._ring_delay_task = asyncio.create_task(
            self._ring_after_delay(delay_seconds)
        )
        logger.info("%d秒後にベルを鳴らします", delay_seconds)
        self._display_state("ring_scheduled", f"{delay_seconds}秒後に呼び出します")

    async def _ring_after_delay(self, delay_seconds: int) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            if not self._hook_up and self.ringer is not None:
                await self.ringer.start()
                self._display_state("ringing", "呼び出し中")
        except asyncio.CancelledError:
            return
        finally:
            if self._ring_delay_task is asyncio.current_task():
                self._ring_delay_task = None

    async def _cancel_ring(self) -> None:
        if self._ring_delay_task is not None:
            self._ring_delay_task.cancel()
            await asyncio.gather(self._ring_delay_task, return_exceptions=True)
            self._ring_delay_task = None
        if self.ringer is not None:
            await self.ringer.stop()

    async def _start_conversation(self) -> None:
        if self.session.is_running or self.state == AppState.CONNECTING:
            return
        self.state = AppState.CONNECTING
        self._display_state("connecting", "接続中")
        try:
            await self.session.start()
            self.state = AppState.CONVERSATION
            self._display_state("conversation", "通話中")
        except Exception:
            self.state = AppState.ERROR
            self._display_state("error", "接続エラー")
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

    def _display_state(self, state: str, label: str) -> None:
        if self.display is not None:
            self.display.publish({"type": "state", "state": state, "label": label})
