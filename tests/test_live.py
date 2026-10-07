import asyncio
import base64
import json
import unittest

from kuro_sagi_denwa.config import Settings
from kuro_sagi_denwa.live import GPTLiveSession


class DummyAudio:
    def __init__(self):
        self.input_queue = asyncio.Queue()
        self.played = []

    async def play(self, data):
        self.played.append(data)


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))


class GPTLiveSessionTest(unittest.TestCase):
    def test_session_start_event_is_minimal_free_conversation(self):
        settings = Settings(
            api_key="test-key",
            live_model="gpt-live-1",
            live_voice="marin",
            live_instructions="明るい声で話してください。",
        )
        session = GPTLiveSession(settings, DummyAudio())

        event = session._session_start_event()

        self.assertEqual(event["type"], "session.start")
        self.assertEqual(event["session"]["model"], "gpt-live-1")
        self.assertEqual(event["session"]["audio"]["format"]["rate"], 24_000)
        self.assertEqual(event["session"]["audio"]["output"]["voice"], "marin")
        self.assertNotIn("input", event["session"])
        self.assertNotIn("store", event["session"])
        self.assertNotIn("delegation", event["session"])
        self.assertIn("日本語で自然に会話", event["session"]["instructions"])
        self.assertIn("明るい声", event["session"]["instructions"])


class GPTLiveSessionAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_audio_forwards_pcm(self):
        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        websocket = FakeWebSocket()
        session.websocket = websocket
        await audio.input_queue.put(b"pcm")

        task = asyncio.create_task(session._send_audio_loop())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(websocket.sent[0]["type"], "session.input_audio.append")
        self.assertEqual(
            websocket.sent[0]["audio"], base64.b64encode(b"pcm").decode("ascii")
        )
        await asyncio.wait_for(audio.input_queue.join(), timeout=0.1)

    async def test_dial_is_ignored(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())
        session.websocket = FakeWebSocket()

        await session.notify_dial(7)

        self.assertEqual(session.websocket.sent, [])


if __name__ == "__main__":
    unittest.main()
