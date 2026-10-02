"""黒詐欺電話アプリケーションのエントリーポイント。"""

from __future__ import annotations

import asyncio
import logging

from .audio import AudioDevice
from .config import Settings, load_env_file
from .controller import PhoneController
from .hardware import MockPhoneHardware, PhoneHardware
from .live import GPTLiveSession


async def run() -> None:
    load_env_file()
    settings = Settings.from_env()
    settings.validate()

    hardware = MockPhoneHardware() if settings.mock_gpio else PhoneHardware(settings)
    audio = AudioDevice(settings)
    session = GPTLiveSession(settings, audio)
    controller = PhoneController(session)

    try:
        async for event in hardware.events():
            await controller.handle(event)
    finally:
        await session.stop()
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
