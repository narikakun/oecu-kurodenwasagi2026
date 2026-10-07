import unittest

from kuro_sagi_denwa.config import Settings
from kuro_sagi_denwa.hardware import PhoneHardware


class PhoneHardwareTest(unittest.TestCase):
    def test_dial_pulse_callback_queues_timestamp(self):
        hardware = PhoneHardware(Settings(api_key="test"))

        hardware._on_dial_pulse()

        self.assertGreater(hardware._dial_pulses.get_nowait(), 0)

    def test_defaults_match_verified_wiring(self):
        settings = Settings(api_key="test")

        self.assertEqual(settings.dial_gpio, 26)
        self.assertEqual(settings.dial_debounce_ms, 5)


if __name__ == "__main__":
    unittest.main()
