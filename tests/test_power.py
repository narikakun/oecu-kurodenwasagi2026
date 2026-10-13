import asyncio
import subprocess
import unittest
from unittest.mock import Mock, patch

from kuro_sagi_denwa.audio import AudioDevice, AudioDeviceSelector, BellRinger
from kuro_sagi_denwa.config import Settings
from kuro_sagi_denwa.power import is_raspberry_pi, shutdown_raspberry_pi


class PowerTest(unittest.TestCase):
    def test_detects_pi_from_linux_device_tree(self):
        with patch("kuro_sagi_denwa.power.sys.platform", "linux"), \
             patch("kuro_sagi_denwa.power.Path.read_bytes", return_value=b"Raspberry Pi 5 Model B\0"):
            self.assertTrue(is_raspberry_pi())

    def test_other_linux_machine_and_mac_are_not_pi(self):
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), \
                 patch("kuro_sagi_denwa.power.sys.platform", platform), \
                 patch("kuro_sagi_denwa.power.Path.read_bytes", return_value=b"Other computer"):
                self.assertFalse(is_raspberry_pi())

    def test_shutdown_command_is_noninteractive_and_pi_only(self):
        with patch("kuro_sagi_denwa.power.is_raspberry_pi", return_value=True), \
             patch("kuro_sagi_denwa.power.subprocess.run") as run:
            shutdown_raspberry_pi()
        run.assert_called_once_with(
            ["sudo", "-n", "/usr/sbin/shutdown", "-h", "now"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        with patch("kuro_sagi_denwa.power.is_raspberry_pi", return_value=False), \
             patch("kuro_sagi_denwa.power.subprocess.run") as run:
            with self.assertRaises(RuntimeError):
                shutdown_raspberry_pi()
            run.assert_not_called()


class ShutdownMenuTest(unittest.IsolatedAsyncioTestCase):
    def make_selector(self, pi=True):
        settings = Settings(api_key="test")
        with patch("kuro_sagi_denwa.audio.is_raspberry_pi", return_value=pi):
            selector = AudioDeviceSelector(AudioDevice(settings), BellRinger(settings), Mock())
        selector._devices = lambda: []
        selector.enter()
        return selector

    async def test_confirm_and_cancel_shutdown(self):
        selector = self.make_selector()
        with patch("kuro_sagi_denwa.audio.shutdown_raspberry_pi") as shutdown:
            selector.handle_digit(8)
            selector.handle_digit(0)
            shutdown.assert_not_called()
            selector.handle_digit(7)
            self.assertIsNone(selector._pending_target)
            selector.handle_digit(9)
            shutdown.assert_not_called()
            selector.handle_digit(8)
            selector.handle_digit(9)
            selector.handle_digit(9)
            await selector._shutdown_task
            selector.handle_digit(9)
            shutdown.assert_called_once_with()

    async def test_non_pi_does_not_offer_or_run_shutdown(self):
        selector = self.make_selector(pi=False)
        choices = selector.display.publish.call_args.args[0]["choices"]
        self.assertFalse(any(choice.startswith("8 ") for choice in choices))
        with patch("kuro_sagi_denwa.audio.shutdown_raspberry_pi") as shutdown:
            selector.handle_digit(8)
            selector.handle_digit(9)
            self.assertIsNone(selector._shutdown_task)
            shutdown.assert_not_called()

    async def test_permission_failure_is_displayed_and_can_retry(self):
        selector = self.make_selector()
        with patch("kuro_sagi_denwa.audio.shutdown_raspberry_pi", side_effect=subprocess.CalledProcessError(1, "sudo")):
            selector.handle_digit(8)
            selector.handle_digit(9)
            with self.assertLogs("kuro_sagi_denwa.audio", level="ERROR"):
                await selector._shutdown_task
        event = selector.display.publish.call_args.args[0]
        self.assertEqual(event["title"], "電源を切れませんでした")
        selector.handle_digit(8)
        self.assertEqual(selector._pending_kind, "shutdown")
