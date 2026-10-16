import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from kuro_sagi_denwa import main


class MainRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_background_live_failure_escapes_run_for_systemd_restart(self):
        async def hardware_events():
            await asyncio.Event().wait()
            yield  # pragma: no cover

        hardware = SimpleNamespace(events=hardware_events, close=Mock())
        display = SimpleNamespace(
            start=AsyncMock(), start_kiosk=AsyncMock(), reset=Mock(), stop=AsyncMock()
        )
        ringer = SimpleNamespace(stop=AsyncMock())
        selector = SimpleNamespace(
            restore=Mock(), remember_current_names=Mock(), show_current=Mock()
        )
        archive = SimpleNamespace(start=Mock(), stop=AsyncMock())
        session = SimpleNamespace(
            wait_for_failure=AsyncMock(
                side_effect=RuntimeError("GPT-Liveのバックグラウンド処理が停止しました")
            ),
            stop=AsyncMock(),
        )
        controller = SimpleNamespace(close=AsyncMock(), handle=AsyncMock())
        settings = SimpleNamespace(
            mock_gpio=True,
            display_kiosk=False,
            display_kiosk_browser="",
            transcript_api_url="",
            transcript_api_token="",
            transcript_device_name="test",
            validate=Mock(),
        )

        with (
            patch.object(main, "load_env_file"),
            patch.object(main.Settings, "from_env", return_value=settings),
            patch.object(main, "MockPhoneHardware", return_value=hardware),
            patch.object(main, "DisplayServer", return_value=display),
            patch.object(main, "AudienceOutput", return_value=Mock()),
            patch.object(main, "AudioDevice", return_value=Mock()),
            patch.object(main, "BellRinger", return_value=ringer),
            patch.object(main, "AudioDeviceSelector", return_value=selector),
            patch.object(main, "TranscriptArchive", return_value=archive),
            patch.object(main, "GPTLiveSession", return_value=session),
            patch.object(main, "PhoneController", return_value=controller),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "GPT-Liveのバックグラウンド処理が停止しました"
            ):
                await main.run()

        controller.close.assert_awaited_once()
        ringer.stop.assert_awaited_once()
        session.stop.assert_awaited_once()
        archive.stop.assert_awaited_once()
        display.stop.assert_awaited_once()
        hardware.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
