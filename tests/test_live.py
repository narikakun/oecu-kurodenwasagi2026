import asyncio
import base64
import json
import unittest
from unittest.mock import patch

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

    def test_greeting_is_only_moshimoshi(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())

        event = session._greeting_event()

        self.assertEqual(event["type"], "session.instructions.append")
        self.assertIsNone(event["delegation_id"])
        self.assertIn("「もしもし？」とだけ", event["content"])
        self.assertIn("相手の返答を待って", event["content"])


class GPTLiveSessionAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_logs_only_input_and_output_transcripts(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())

        with self.assertLogs("kuro_sagi_denwa.transcript", level="INFO") as logs:
            self.assertTrue(
                session._log_transcript(
                    {"type": "session.input_transcript.delta", "delta": "こんにちは"}
                )
            )
            self.assertTrue(
                session._log_transcript(
                    {"type": "session.input_transcript.delta", "delta": "、元気ですか？"}
                )
            )
            self.assertTrue(
                session._log_transcript(
                    {"type": "session.output_transcript.delta", "delta": "もしもし"}
                )
            )
            session._flush_all_transcripts()

        self.assertIn("文字起こし中（参加者）: こんにちは", logs.output[0])
        self.assertIn("文字起こし中（AI）: もしもし", logs.output[1])
        self.assertIn("参加者: こんにちは、元気ですか？", logs.output[2])
        self.assertIn("AI: もしもし", logs.output[3])

    async def test_does_not_log_audio_base64(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())

        with patch("kuro_sagi_denwa.live.transcript_logger.info") as log_info:
            logged = session._log_transcript(
                {"type": "session.output_audio.delta", "delta": "YmFzZTY0"}
            )

        self.assertFalse(logged)
        log_info.assert_not_called()

    async def test_reports_live_error_without_dumping_event(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())

        task = asyncio.create_task(
            self._raise_error(RuntimeError("GPT-Liveエラー [bad_request]: invalid"))
        )
        await asyncio.gather(task, return_exceptions=True)

        with self.assertLogs("kuro_sagi_denwa.live", level="ERROR") as logs:
            session._report_receiver_failure(task)

        self.assertIn("GPT-Liveの受信処理が停止しました", logs.output[0])
        self.assertIn("bad_request", logs.output[0])

    @staticmethod
    async def _raise_error(error):
        raise error

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
