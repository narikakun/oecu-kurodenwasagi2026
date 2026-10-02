import unittest

from kuro_sagi_denwa.config import Settings
from kuro_sagi_denwa.live import GPTLiveSession
from kuro_sagi_denwa.scenario import FraudScenario


class DummyAudio:
    pass


class GPTLiveSessionTest(unittest.TestCase):
    def test_session_start_event_uses_expected_live_settings(self):
        settings = Settings(
            api_key="test-key",
            live_model="gpt-live-1",
            live_voice="marin",
            live_instructions="日本語で話してください。",
        )
        scenario = FraudScenario(ticket_number="547")
        session = GPTLiveSession(settings, DummyAudio(), scenario)

        event = session._session_start_event()

        self.assertEqual(event["type"], "session.start")
        self.assertEqual(event["session"]["model"], "gpt-live-1")
        self.assertEqual(event["session"]["audio"]["format"]["rate"], 24_000)
        self.assertEqual(event["session"]["audio"]["output"]["voice"], "marin")
        self.assertFalse(event["session"]["store"])
        self.assertIn("もしもし", event["session"]["instructions"])
        self.assertIn("短時間で終わります", event["session"]["instructions"])
        self.assertIn("これは緊急のご連絡です", event["session"]["instructions"])
        self.assertIn("五、四、七", event["session"]["instructions"])

    def test_event_ids_are_unique(self):
        settings = Settings(api_key="test-key")
        session = GPTLiveSession(
            settings, DummyAudio(), FraudScenario(ticket_number="547")
        )

        self.assertNotEqual(
            session._next_event_id("dial_7"), session._next_event_id("dial_7")
        )


if __name__ == "__main__":
    unittest.main()
