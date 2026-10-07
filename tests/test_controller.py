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


class FakeRinger:
    def __init__(self):
        self.start_count = 0
        self.stop_count = 0

    @property
    def is_ringing(self):
        return self.start_count > self.stop_count

    async def start(self):
        self.start_count += 1

    async def stop(self):
        self.stop_count += 1


class FakeDeviceSelector:
    def __init__(self):
        self.active = False
        self.enter_count = 0
        self.exit_count = 0
        self.digits = []

    def enter(self):
        self.active = True
        self.enter_count += 1

    def exit(self):
        self.active = False
        self.exit_count += 1

    def handle_digit(self, digit):
        self.digits.append(digit)


class FakeDisplay:
    def __init__(self):
        self.events = []
        self.reset_count = 0

    def publish(self, event):
        self.events.append(event)

    def reset(self):
        self.reset_count += 1
        self.events.clear()


class PhoneControllerTest(unittest.IsolatedAsyncioTestCase):
    async def test_hook_starts_and_stops_conversation(self):
        session = FakeSession()
        display = FakeDisplay()
        controller = PhoneController(session, display=display)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        self.assertEqual(controller.state, AppState.CONVERSATION)
        self.assertEqual(session.start_count, 1)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))
        self.assertEqual(controller.state, AppState.IDLE)
        self.assertEqual(session.stop_count, 1)
        self.assertEqual(display.reset_count, 1)
        self.assertEqual(display.events, [])

    async def test_duplicate_hook_up_does_not_start_twice(self):
        session = FakeSession()
        controller = PhoneController(session)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))

        self.assertEqual(session.start_count, 1)

    async def test_dial_value_is_recorded(self):
        session = FakeSession()
        controller = PhoneController(session)
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 7))
        self.assertEqual(controller.last_digit, 7)
        self.assertEqual(session.digits, [7])

    async def test_on_hook_dial_zero_starts_bell(self):
        session = FakeSession()
        ringer = FakeRinger()
        controller = PhoneController(session, ringer)
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))

        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 0))
        ring_task = controller._ring_delay_task
        self.assertIsNotNone(ring_task)
        await ring_task

        self.assertEqual(ringer.start_count, 1)
        self.assertEqual(session.start_count, 0)

    async def test_lifting_handset_cancels_bell_and_starts_conversation(self):
        session = FakeSession()
        ringer = FakeRinger()
        controller = PhoneController(session, ringer)
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))
        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 9))

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))

        self.assertEqual(ringer.start_count, 0)
        self.assertGreaterEqual(ringer.stop_count, 1)
        self.assertEqual(session.start_count, 1)

    async def test_second_on_hook_digit_replaces_previous_reservation(self):
        session = FakeSession()
        ringer = FakeRinger()
        controller = PhoneController(session, ringer)
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))
        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 9))

        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 0))
        ring_task = controller._ring_delay_task
        self.assertIsNotNone(ring_task)
        await ring_task

        self.assertEqual(ringer.start_count, 1)

    async def test_dial_while_ringing_enters_device_settings(self):
        session = FakeSession()
        ringer = FakeRinger()
        selector = FakeDeviceSelector()
        controller = PhoneController(session, ringer, selector)
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))
        await ringer.start()

        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 2))

        self.assertEqual(ringer.stop_count, 1)
        self.assertEqual(selector.enter_count, 1)
        self.assertEqual(selector.digits, [2])

    async def test_hook_up_exits_device_settings(self):
        session = FakeSession()
        ringer = FakeRinger()
        selector = FakeDeviceSelector()
        selector.enter()
        controller = PhoneController(session, ringer, selector)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))

        self.assertFalse(selector.active)
        self.assertEqual(selector.exit_count, 1)
        self.assertEqual(session.start_count, 1)


if __name__ == "__main__":
    unittest.main()
