import asyncio
import base64
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from kuro_sagi_denwa.config import Settings
from kuro_sagi_denwa.live import GPTLiveSession


class DummyAudio:
    def __init__(self):
        self.input_queue = asyncio.Queue()
        self.played = []
        self.wait_count = 0
        self.clear_count = 0
        self.start_count = 0
        self.stop_count = 0

    async def start(self):
        self.start_count += 1

    async def stop(self):
        self.stop_count += 1

    async def play(self, data):
        self.played.append(data)

    async def wait_until_played(self):
        self.wait_count += 1

    def clear_input_queue(self):
        self.clear_count += 1


class FakeWebSocket:
    def __init__(self, received=None):
        self.sent = []
        self.received = list(received or [])
        self.close_count = 0

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def recv(self):
        event = self.received.pop(0)
        if isinstance(event, BaseException):
            raise event
        return json.dumps(event)

    async def close(self):
        self.close_count += 1


class FakeDisplay:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


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
        self.assertEqual(event["session"]["delegation"]["type"], "responses")
        self.assertEqual(
            event["session"]["delegation"]["responses"]["model"], "gpt-6-luna"
        )
        self.assertEqual(
            event["session"]["delegation"]["responses"]["tools"],
            [{"type": "web_search"}],
        )
        self.assertEqual(
            event["session"]["delegation"]["responses"]["tool_choice"], "auto"
        )
        self.assertIn("日本語で自然に会話", event["session"]["instructions"])
        self.assertIn("一度の発話は原則1〜2文", event["session"]["instructions"])
        self.assertIn("質問は1つだけ", event["session"]["instructions"])
        self.assertIn("毎回質問で返さない", event["session"]["instructions"])
        self.assertIn("相手が話し始めたら発話を止め", event["session"]["instructions"])
        self.assertIn("明るい声", event["session"]["instructions"])
        self.assertIn("守口市役所", event["session"]["instructions"])
        self.assertIn("22,560円", event["session"]["instructions"])
        self.assertIn("お手続きを希望されますか", event["session"]["instructions"])
        self.assertIn("具体的な送金操作へは進まない", event["session"]["instructions"])

    def test_web_search_can_be_disabled(self):
        settings = Settings(api_key="test-key", live_web_search=False)
        session = GPTLiveSession(settings, DummyAudio())

        responses = session._session_start_event()["session"]["delegation"]["responses"]

        self.assertNotIn("tools", responses)
        self.assertNotIn("tool_choice", responses)

    def test_scenario_dates_are_built_when_session_event_is_created(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())

        with patch(
            "kuro_sagi_denwa.live.build_refund_fraud_scenario",
            side_effect=["1回目の日付", "2回目の日付"],
        ) as build_scenario:
            first = session._session_start_event()
            second = session._session_start_event()

        self.assertIn("1回目の日付", first["session"]["instructions"])
        self.assertIn("2回目の日付", second["session"]["instructions"])
        self.assertEqual(build_scenario.call_count, 2)

    def test_greeting_is_only_moshimoshi(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())

        event = session._greeting_event()

        self.assertEqual(event["type"], "session.commentary.append")
        self.assertIsNone(event["delegation_id"])
        self.assertEqual(event["content"], "もしもし？")


class GPTLiveSessionAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_logs_session_start(self):
        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        websocket = FakeWebSocket(
            [
                {"type": "session.started"},
                {
                    "type": "session.input_audio.muted",
                    "client_event_id": "pre_ring_mute",
                },
            ]
        )

        fake_websockets = SimpleNamespace(
            connect=AsyncMock(return_value=websocket)
        )
        with patch.dict(sys.modules, {"websockets": fake_websockets}):
            with self.assertLogs("kuro_sagi_denwa.live", level="INFO") as logs:
                await session.prepare()

        self.assertIn("GPT-Liveセッションへ接続します", logs.output[0])
        self.assertIn("GPT-Liveセッションを開始しました", logs.output[-1])
        self.assertTrue(session._prepared)

    async def test_waits_for_initial_greeting_before_conversation(self):
        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        session.websocket = FakeWebSocket(
            [
                {
                    "type": "session.output_audio.delta",
                    "delta": base64.b64encode(b"moshimoshi").decode("ascii"),
                },
                {
                    "type": "session.commentary.appended",
                    "client_event_id": "initial_greeting",
                },
                {"type": "session.output_transcript.delta", "delta": "もしもし？"},
            ]
        )

        await session._wait_until_greeting_accepted()

        self.assertEqual(audio.played, [b"moshimoshi"])
        self.assertEqual(audio.wait_count, 1)

    async def test_waits_for_matching_audio_control_acknowledgement(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())
        session.websocket = FakeWebSocket(
            [
                {
                    "type": "session.input_audio.muted",
                    "client_event_id": "different_event",
                },
                {
                    "type": "session.input_audio.muted",
                    "client_event_id": "pre_ring_mute",
                },
            ]
        )

        await session._wait_for_ack("session.input_audio.muted", "pre_ring_mute")

    async def test_stop_closes_prepared_session_without_receiver_loop(self):
        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        websocket = FakeWebSocket([{"type": "session.closed"}])
        session.websocket = websocket
        session._prepared = True

        with self.assertLogs("kuro_sagi_denwa.live", level="INFO") as logs:
            await session.stop()

        self.assertEqual(websocket.sent, [{"type": "session.close"}])
        self.assertEqual(websocket.close_count, 1)
        self.assertEqual(audio.stop_count, 1)
        self.assertIsNone(session.websocket)
        self.assertFalse(session._prepared)
        self.assertIn("GPT-Liveセッションを終了します", logs.output[0])
        self.assertIn("GPT-Liveセッションを終了しました", logs.output[-1])

    async def test_cancelled_start_closes_connection_and_audio(self):
        class BlockingWebSocket(FakeWebSocket):
            async def recv(self):
                if not self.received:
                    await asyncio.Event().wait()
                return await super().recv()

        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        websocket = BlockingWebSocket(
            [
                {
                    "type": "session.input_audio.unmuted",
                    "client_event_id": "handset_up_unmute",
                }
            ]
        )
        session.websocket = websocket
        session._prepared = True

        start_task = asyncio.create_task(session.start())
        for _ in range(10):
            await asyncio.sleep(0)
        start_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await start_task

        self.assertEqual(websocket.close_count, 1)
        self.assertIsNone(session.websocket)
        self.assertIsNone(session._sender_task)
        self.assertFalse(session.is_running)
        self.assertEqual(audio.stop_count, 1)

    async def test_commentary_acknowledgement_alone_does_not_finish_greeting(self):
        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        session.websocket = FakeWebSocket(
            [
                {
                    "type": "session.commentary.appended",
                    "client_event_id": "initial_greeting",
                },
                {
                    "type": "session.output_audio.delta",
                    "delta": base64.b64encode(b"moshimoshi").decode("ascii"),
                },
                {"type": "session.output_transcript.delta", "delta": "もしもし"},
            ]
        )

        await session._wait_until_greeting_accepted()

        self.assertEqual(audio.played, [b"moshimoshi"])
        self.assertEqual(audio.wait_count, 1)

    async def test_retries_greeting_once_when_no_speech_arrives(self):
        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        session.websocket = FakeWebSocket(
            [
                {
                    "type": "session.commentary.appended",
                    "client_event_id": "initial_greeting",
                },
                asyncio.TimeoutError(),
                {
                    "type": "session.commentary.appended",
                    "client_event_id": "initial_greeting_retry",
                },
                {
                    "type": "session.output_audio.delta",
                    "delta": base64.b64encode(b"moshimoshi").decode("ascii"),
                },
                {"type": "session.output_transcript.delta", "delta": "もしもし"},
            ]
        )

        with self.assertLogs("kuro_sagi_denwa.live", level="WARNING") as logs:
            await session._wait_until_greeting_accepted()

        self.assertEqual(
            session.websocket.sent,
            [session._greeting_event("initial_greeting_retry")],
        )
        self.assertIn("挨拶を再送します", logs.output[0])

    async def test_logs_only_input_and_output_transcripts(self):
        display = FakeDisplay()
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio(), display)

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
        self.assertEqual(display.events[0]["status"], "partial")
        self.assertEqual(display.events[1]["text"], "こんにちは、元気ですか？")
        self.assertEqual(display.events[-1]["status"], "final")

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
