"""黒詐欺電話アプリケーションのエントリーポイント。"""

from __future__ import annotations

import asyncio
import logging

from .audio import AudioDevice, AudioDeviceSelector, BellRinger
from .config import Settings, load_env_file
from .controller import PhoneController
from .display import DisplayServer
from .hardware import MockPhoneHardware, PhoneHardware
from .live import GPTLiveSession


async def run() -> None:
    load_env_file()
    settings = Settings.from_env()
    settings.validate()

    hardware = MockPhoneHardware() if settings.mock_gpio else PhoneHardware(settings)
    display = DisplayServer()
    await display.start()
    if settings.display_kiosk:
        await display.start_kiosk(settings.display_kiosk_browser)
    audio = AudioDevice(settings)
    ringer = BellRinger(settings)
    device_selector = AudioDeviceSelector(audio, ringer, display)
    device_selector.restore()
    device_selector.show_current()
    display.reset()
    session = GPTLiveSession(settings, audio, display)
    controller = PhoneController(session, ringer, device_selector, display)

    try:
        async for event in hardware.events():
            await controller.handle(event)
    finally:
        await ringer.stop()
        await session.stop()
        await display.stop()
        hardware.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except ValueError as error:
        logging.getLogger(__name__).error("設定エラー: %s", error)


if __name__ == "__main__":
    main()
