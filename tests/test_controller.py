import unittest

from kuro_sagi_denwa.controller import AppState, PhoneController
from kuro_sagi_denwa.hardware import HardwareEvent, HardwareEventType


class FakeSession:
    def __init__(self):
        self.running = False
        self.start_count = 0
        self.stop_count = 0
        self.digits = []

    @property
    def is_running(self):
        return self.running

    async def start(self):
        self.running = True
        self.start_count += 1

    async def stop(self):
        self.running = False
        self.stop_count += 1

    async def notify_dial(self, digit):
        self.digits.append(digit)


class PhoneControllerTest(unittest.IsolatedAsyncioTestCase):
    async def test_hook_starts_and_stops_conversation(self):
        session = FakeSession()
        controller = PhoneController(session)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        self.assertEqual(controller.state, AppState.CONVERSATION)
        self.assertEqual(session.start_count, 1)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))
        self.assertEqual(controller.state, AppState.IDLE)
        self.assertEqual(session.stop_count, 1)

    async def test_duplicate_hook_up_does_not_start_twice(self):
        session = FakeSession()
        controller = PhoneController(session)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))

        self.assertEqual(session.start_count, 1)

    async def test_dial_value_is_recorded(self):
        session = FakeSession()
        session.running = True
        controller = PhoneController(session)
        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 7))
        self.assertEqual(controller.last_digit, 7)
        self.assertEqual(session.digits, [7])


if __name__ == "__main__":
    unittest.main()
