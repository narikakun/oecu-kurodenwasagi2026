import asyncio
import unittest

from kuro_sagi_denwa.controller import AppState, PhoneController
from kuro_sagi_denwa.hardware import HardwareEvent, HardwareEventType


class FakeSession:
    def __init__(self):
        self.running = False
        self.start_count = 0
        self.stop_count = 0
        self.prepare_count = 0
        self.digits = []
        self.active = False
        self.feedback_active = False
        self.feedback_done = asyncio.Event()
        self.continue_feedback_count = 0

    @property
    def is_running(self):
        return self.running

    @property
    def is_conversation_active(self):
        return self.active

    @property
    def is_feedback_active(self):
        return self.feedback_active

    async def prepare(self):
        self.running = True
        self.prepare_count += 1

    async def start(self):
        self.running = True
        self.active = True
        self.start_count += 1

    async def stop(self):
        self.running = False
        self.active = False
        self.stop_count += 1

    async def notify_dial(self, digit):
        self.digits.append(digit)

    async def continue_feedback_after_hangup(self):
        self.continue_feedback_count += 1

    async def wait_for_feedback_completion(self):
        await self.feedback_done.wait()


class FakeRinger:
    def __init__(self, output_available=True):
        self.start_count = 0
        self.stop_count = 0
        self.has_output = output_available
        self.stopped = asyncio.Event()

    @property
    def is_ringing(self):
        return self.start_count > self.stop_count

    def output_available(self):
        return self.has_output

    async def start(self):
        self.start_count += 1

    async def wait_until_stopped(self):
        await self.stopped.wait()

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
    async def test_hook_down_keeps_feedback_running_until_it_finishes(self):
        session = FakeSession()
        session.running = True
        session.active = True
        session.feedback_active = True
        display = FakeDisplay()
        controller = PhoneController(session, display=display)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))

        self.assertEqual(controller.state, AppState.FEEDBACK)
        self.assertEqual(session.stop_count, 0)
        self.assertEqual(session.continue_feedback_count, 1)
        self.assertEqual(display.reset_count, 0)

        session.feedback_done.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(controller.state, AppState.IDLE)
        self.assertEqual(session.stop_count, 1)
        self.assertEqual(display.reset_count, 1)

    async def test_any_dial_stops_feedback_without_starting_ring(self):
        session = FakeSession()
        session.running = True
        session.active = True
        session.feedback_active = True
        ringer = FakeRinger()
        display = FakeDisplay()
        controller = PhoneController(session, ringer, display=display)
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))

        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 4))

        self.assertEqual(controller.state, AppState.IDLE)
        self.assertEqual(session.stop_count, 1)
        self.assertIsNone(controller._ring_delay_task)
        self.assertEqual(ringer.start_count, 0)
        self.assertEqual(display.reset_count, 1)

    async def test_dial_stops_feedback_while_handset_is_still_up(self):
        session = FakeSession()
        session.running = True
        session.active = True
        session.feedback_active = True
        controller = PhoneController(session)
        controller.state = AppState.CONVERSATION
        controller._hook_up = True

        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 8))

        self.assertEqual(controller.state, AppState.IDLE)
        self.assertEqual(session.stop_count, 1)
        self.assertEqual(session.digits, [])

    async def test_hook_starts_and_stops_conversation(self):
        session = FakeSession()
        display = FakeDisplay()
        controller = PhoneController(session, display=display)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await controller.wait_until_started()
        self.assertEqual(controller.state, AppState.CONVERSATION)
        self.assertEqual(session.start_count, 1)
        self.assertEqual(display.events[0]["state"], "connecting")
        self.assertEqual(display.events[-1]["state"], "conversation")

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))
        self.assertEqual(controller.state, AppState.IDLE)
        self.assertEqual(session.stop_count, 1)
        self.assertEqual(display.reset_count, 1)
        self.assertEqual(display.events, [])

    async def test_duplicate_hook_up_does_not_start_twice(self):
        session = FakeSession()
        controller = PhoneController(session)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await controller.wait_until_started()
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await controller.wait_until_started()

        self.assertEqual(session.start_count, 1)

    async def test_dial_value_is_recorded(self):
        session = FakeSession()
        controller = PhoneController(session)
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await controller.wait_until_started()
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
        self.assertEqual(session.prepare_count, 1)
        self.assertEqual(session.start_count, 0)

    async def test_missing_bell_output_opens_device_settings(self):
        session = FakeSession()
        ringer = FakeRinger(output_available=False)
        selector = FakeDeviceSelector()
        display = FakeDisplay()
        controller = PhoneController(session, ringer, selector, display)

        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 3))

        self.assertIsNone(controller._ring_delay_task)
        self.assertEqual(session.prepare_count, 0)
        self.assertEqual(ringer.start_count, 0)
        self.assertEqual(selector.enter_count, 1)
        self.assertTrue(selector.active)
        self.assertEqual(display.events[0]["title"], "音声設定")
        self.assertEqual(display.events[0]["subtitle"], "読み込み中…")
        self.assertEqual(display.events[-1]["state"], "settings")

    async def test_bell_playback_failure_opens_settings_and_stops_prepared_session(self):
        class FailingRinger(FakeRinger):
            async def wait_until_stopped(self):
                raise ValueError("Sample format not supported")

        for ringer in (FailingRinger(), FakeRinger()):
            with self.subTest(ringer=type(ringer).__name__):
                session = FakeSession()
                selector = FakeDeviceSelector()
                display = FakeDisplay()
                controller = PhoneController(session, ringer, selector, display)
                await controller.handle(HardwareEvent(HardwareEventType.DIAL, 0))
                await controller._ring_delay_task
                watch_task = controller._ring_watch_task
                ringer.stopped.set()
                await asyncio.wait_for(watch_task, timeout=1)

                self.assertTrue(selector.active)
                self.assertEqual(selector.enter_count, 1)
                self.assertFalse(session.is_running)
                self.assertEqual(session.stop_count, 1)
                self.assertEqual(display.events[-1]["state"], "settings")
                await controller.close()

    async def test_prepares_live_session_during_ring_delay(self):
        session = FakeSession()
        ringer = FakeRinger()
        controller = PhoneController(session, ringer)

        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 3))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(session.prepare_count, 1)
        self.assertEqual(ringer.start_count, 0)
        await controller.close()

    async def test_lifting_handset_cancels_bell_and_starts_conversation(self):
        session = FakeSession()
        ringer = FakeRinger()
        controller = PhoneController(session, ringer)
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))
        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 9))

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await controller.wait_until_started()

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
        display = FakeDisplay()
        controller = PhoneController(session, ringer, selector, display)
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))
        await session.prepare()
        await ringer.start()

        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 2))

        self.assertEqual(display.events[0]["title"], "音声設定")
        self.assertEqual(display.events[0]["subtitle"], "読み込み中…")
        self.assertEqual(display.events[0]["footer"], "しばらくお待ちください")
        self.assertEqual(ringer.stop_count, 1)
        self.assertFalse(session.is_running)
        self.assertEqual(session.stop_count, 1)
        self.assertEqual(selector.enter_count, 1)
        self.assertEqual(selector.digits, [])

        await controller.handle(HardwareEvent(HardwareEventType.DIAL, 2))

        self.assertEqual(selector.enter_count, 1)
        self.assertEqual(selector.digits, [2])

    async def test_hook_up_exits_device_settings(self):
        session = FakeSession()
        ringer = FakeRinger()
        selector = FakeDeviceSelector()
        selector.enter()
        controller = PhoneController(session, ringer, selector)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await controller.wait_until_started()

        self.assertFalse(selector.active)
        self.assertEqual(selector.exit_count, 1)
        self.assertEqual(session.start_count, 1)

    async def test_hook_down_during_start_cancels_it_immediately(self):
        class SlowSession(FakeSession):
            def __init__(self):
                super().__init__()
                self.start_cancelled = False

            async def start(self):
                self.running = True
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # 実装のGPTLiveSessionと同じく、中止時は接続を片付ける。
                    self.running = False
                    self.start_cancelled = True
                    raise

        session = SlowSession()
        controller = PhoneController(session)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await asyncio.sleep(0)
        self.assertEqual(controller.state, AppState.CONNECTING)

        await asyncio.wait_for(
            controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN)), timeout=1
        )

        self.assertTrue(session.start_cancelled)
        self.assertEqual(controller.state, AppState.IDLE)

    async def test_hook_up_again_after_cancelled_start_restarts(self):
        session = FakeSession()
        controller = PhoneController(session)

        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_DOWN))
        await controller.handle(HardwareEvent(HardwareEventType.HOOK_UP))
        await controller.wait_until_started()

        self.assertEqual(controller.state, AppState.CONVERSATION)
        self.assertEqual(session.start_count, 1)


if __name__ == "__main__":
    unittest.main()
