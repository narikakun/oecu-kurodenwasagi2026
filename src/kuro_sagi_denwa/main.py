"""黒詐欺電話アプリケーションのエントリーポイント。"""

from __future__ import annotations

import asyncio
import logging
import os

from .audio import AudioDevice, AudioDeviceSelector, BellRinger
from .config import Settings, load_env_file
from .controller import PhoneController
from .display import DisplayServer
from .hardware import HardwareEvent, MockPhoneHardware, PhoneHardware
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

    # GPIOの監視は会話制御とは別タスクで回し続け、
    # 接続や終了を待つ間もフックとダイヤルの変化を取りこぼさないようにする。
    events: asyncio.Queue[HardwareEvent] = asyncio.Queue()

    async def watch_hardware() -> None:
        async for event in hardware.events():
            events.put_nowait(event)

    watcher = asyncio.create_task(watch_hardware())
    try:
        while True:
            get_event = asyncio.create_task(events.get())
            done, _ = await asyncio.wait(
                {get_event, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            if get_event not in done:
                get_event.cancel()
                # GPIO監視が例外で止まった場合は、その例外でアプリを終了させる。
                watcher.result()
                return
            await controller.handle(get_event.result())
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await controller.close()
        await ringer.stop()
        await session.stop()
        await display.stop()
        hardware.close()


def main() -> None:
    # .envのLOG_LEVELもログ設定に反映する。書式エラーはrun()で改めて報告する。
    try:
        load_env_file()
    except ValueError:
        pass
    # ダイヤルのパルス間隔などを確認するときは LOG_LEVEL=DEBUG で起動する。
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
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
