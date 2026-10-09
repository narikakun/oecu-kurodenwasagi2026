"""黒電話のGPIO入力を監視する。"""

from __future__ import annotations

import asyncio
import logging
from queue import Empty, SimpleQueue
from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Protocol

from .config import Settings
from .dial import DialDecoder

logger = logging.getLogger(__name__)


class HardwareEventType(Enum):
    HOOK_UP = "hook_up"
    HOOK_DOWN = "hook_down"
    DIAL = "dial"


@dataclass(frozen=True)
class HardwareEvent:
    type: HardwareEventType
    digit: int | None = None


class DigitalInput(Protocol):
    @property
    def value(self) -> int: ...

    def close(self) -> None: ...


class PhoneHardware:
    """2本のGPIOをポーリングし、意味のある電話イベントへ変換する。"""

    def __init__(
        self,
        settings: Settings,
        hook_input: DigitalInput | None = None,
        dial_input: DigitalInput | None = None,
    ) -> None:
        self.settings = settings
        self.hook_input = hook_input
        self.dial_input = dial_input
        self.decoder = DialDecoder(
            digit_timeout_seconds=settings.digit_timeout_ms / 1000,
        )
        self._dial_pulses: SimpleQueue[float] = SimpleQueue()
        self._owns_inputs = hook_input is None and dial_input is None

    def open(self) -> None:
        if self.hook_input is not None and self.dial_input is not None:
            return
        if self.settings.mock_gpio:
            raise RuntimeError("MOCK_GPIOではMockPhoneHardwareを使用してください")

        # gpiozeroをここで読み込むことで、開発PCでも単体テストしやすくする。
        from gpiozero import Button, DigitalInputDevice

        self.hook_input = DigitalInputDevice(self.settings.hook_gpio, pull_up=True)
        self.dial_input = Button(
            self.settings.dial_gpio,
            pull_up=True,
            bounce_time=self.settings.dial_debounce_ms / 1000,
        )
        self.dial_input.when_pressed = self._on_dial_pulse

    def _on_dial_pulse(self) -> None:
        """gpiozeroのコールバックスレッドからパルス時刻だけを渡す。"""
        self._dial_pulses.put(monotonic())

    def _hook_is_up(self) -> bool:
        assert self.hook_input is not None
        is_low = self.hook_input.value == 0
        return is_low == self.settings.hook_lifted_when_low

    async def events(self):
        self.open()
        assert self.hook_input is not None and self.dial_input is not None

        hook_up = self._hook_is_up()
        hook_candidate = hook_up
        hook_changed_at = monotonic()

        # 起動時点の状態も通知し、アプリ側と実機の状態を一致させる。
        yield HardwareEvent(
            HardwareEventType.HOOK_UP if hook_up else HardwareEventType.HOOK_DOWN
        )

        while True:
            now = monotonic()
            current_hook = self._hook_is_up()

            if current_hook != hook_candidate:
                hook_candidate = current_hook
                hook_changed_at = now
            elif current_hook != hook_up:
                if now - hook_changed_at >= self.settings.hook_debounce_ms / 1000:
                    hook_up = current_hook
                    if not hook_up:
                        self.decoder.reset()
                    yield HardwareEvent(
                        HardwareEventType.HOOK_UP
                        if hook_up
                        else HardwareEventType.HOOK_DOWN
                    )

            while True:
                try:
                    pulse_at = self._dial_pulses.get_nowait()
                except Empty:
                    break
                self.decoder.add_pulse(pulse_at)

            while (digit := self.decoder.read_digit(now)) is not None:
                yield HardwareEvent(HardwareEventType.DIAL, digit)

            await asyncio.sleep(0.005)

    def close(self) -> None:
        if self._owns_inputs:
            if self.hook_input is not None:
                self.hook_input.close()
            if self.dial_input is not None:
                self.dial_input.close()


class MockPhoneHardware:
    """実機なしで起動確認するための簡単なコンソール入力。"""

    async def events(self):
        logger.info("モックGPIO: u=受話器を上げる, d=置く, 0-9=ダイヤル")
        yield HardwareEvent(HardwareEventType.HOOK_DOWN)
        while True:
            command = (await asyncio.to_thread(input, "phone> ")).strip().lower()
            if command == "u":
                yield HardwareEvent(HardwareEventType.HOOK_UP)
            elif command == "d":
                yield HardwareEvent(HardwareEventType.HOOK_DOWN)
            elif len(command) == 1 and command.isdigit():
                yield HardwareEvent(HardwareEventType.DIAL, int(command))
            else:
                logger.info("u、d、0～9のいずれかを入力してください")

    def close(self) -> None:
        pass
