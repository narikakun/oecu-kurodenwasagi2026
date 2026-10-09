import unittest
from time import monotonic

from kuro_sagi_denwa.config import Settings
from kuro_sagi_denwa.hardware import HardwareEventType, PhoneHardware


class PhoneHardwareTest(unittest.TestCase):
    def test_dial_pulse_callback_queues_timestamp(self):
        hardware = PhoneHardware(Settings(api_key="test"))

        hardware._on_dial_pulse()

        self.assertGreater(hardware._dial_pulses.get_nowait(), 0)

    def test_defaults_match_verified_wiring(self):
        settings = Settings(api_key="test")

        self.assertEqual(settings.dial_gpio, 26)
        self.assertEqual(settings.dial_debounce_ms, 5)


class FakeInput:
    def __init__(self, value=1):
        self.value = value

    def close(self):
        pass


class PhoneHardwareEventsTest(unittest.IsolatedAsyncioTestCase):
    async def test_backlogged_pulses_become_separate_digits(self):
        hardware = PhoneHardware(Settings(api_key="test"), FakeInput(0), FakeInput())
        start = monotonic() - 2
        for index in range(3):
            hardware._dial_pulses.put(start + index * 0.05)
        for index in range(2):
            hardware._dial_pulses.put(start + 0.5 + index * 0.05)

        events = hardware.events()
        first = await anext(events)
        self.assertEqual(first.type, HardwareEventType.HOOK_DOWN)
        digits = [(await anext(events)).digit for _ in range(2)]
        await events.aclose()

        self.assertEqual(digits, [3, 2])


if __name__ == "__main__":
    unittest.main()
