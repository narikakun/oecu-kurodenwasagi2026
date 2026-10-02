import unittest

from kuro_sagi_denwa.config import Settings
from kuro_sagi_denwa.live import GPTLiveSession


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
        session = GPTLiveSession(settings, DummyAudio())

        event = session._session_start_event()

        self.assertEqual(event["type"], "session.start")
        self.assertEqual(event["session"]["model"], "gpt-live-1")
        self.assertEqual(event["session"]["audio"]["format"]["rate"], 24_000)
        self.assertEqual(event["session"]["audio"]["output"]["voice"], "marin")
        self.assertFalse(event["session"]["store"])


if __name__ == "__main__":
    unittest.main()
