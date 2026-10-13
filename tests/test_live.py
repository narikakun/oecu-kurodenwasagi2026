import asyncio
import base64
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from kuro_sagi_denwa.config import Settings
from kuro_sagi_denwa.live import (
    GPTLiveSession,
    normalize_transcript_for_display,
    normalize_transcript_numbers,
)


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


class FakeArchive:
    def __init__(self):
        self.transcripts = []

    def record_transcript(self, speaker, text):
        self.transcripts.append((speaker, text))


class GPTLiveSessionTest(unittest.TestCase):
    def test_normalizes_transcript_numbers_to_half_width_digits(self):
        self.assertEqual(
            normalize_transcript_numbers("過去三年間で二万二千五百六十円です"),
            "過去3年間で22560円です",
        )
        self.assertEqual(
            normalize_transcript_numbers("番号は９１１０、回数は百二十三回です"),
            "番号は9110、回数は123回です",
        )
        self.assertEqual(normalize_transcript_numbers("２万5千円"), "25000円")

    def test_session_instructions_include_reference_pronunciations(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())

        instructions = session._session_start_event()["session"]["instructions"]

        self.assertIn("『守口市役所』は必ず『もりぐちしやくしょ』", instructions)
        self.assertIn("『まもりくち』という読み方は禁止", instructions)
        self.assertIn("『還付金』を『かんぷきん』", instructions)
        self.assertIn("『22,560円』は", instructions)

    def test_uses_official_moriguchi_spelling_only_for_assistant_captions(self):
        spoken = "もりぐち市役所、保険年金課です"

        self.assertEqual(
            normalize_transcript_for_display(spoken, assistant=True),
            "守口市役所、保険年金課です",
        )
        self.assertEqual(
            normalize_transcript_for_display(spoken, assistant=False),
            spoken,
        )

    def test_restores_spoken_refund_amount_to_half_width_digits(self):
        spoken = "にまんにせんごひゃくろくじゅうえんの払い戻しがあります。"

        self.assertEqual(
            normalize_transcript_for_display(spoken, assistant=True),
            "22560円の払い戻しがあります。",
        )
        self.assertEqual(
            normalize_transcript_for_display(spoken, assistant=False),
            spoken,
        )

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

        self.assertEqual(event["type"], "session.instructions.append")
        self.assertIsNone(event["delegation_id"])
        self.assertIn("今すぐあなたから先に", event["content"])
        self.assertIn("「もしもし？」とだけ", event["content"])

        retry_event = session._greeting_event("initial_greeting_retry")
        self.assertIn("まだ最初の挨拶が聞こえていません", retry_event["content"])


class GPTLiveSessionAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_display_transcript_uses_half_width_digits_for_each_update(self):
        display = FakeDisplay()
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio(), display)

        session._log_transcript(
            {"type": "session.output_transcript.delta", "delta": "金額は二万"}
        )
        session._log_transcript(
            {"type": "session.output_transcript.delta", "delta": "二千五百六十円です。"}
        )
        session._flush_all_transcripts()

        self.assertEqual(display.events[0]["text"], "金額は20000")
        self.assertEqual(display.events[1]["text"], "金額は22560円です。")
        self.assertEqual(display.events[-1]["text"], "金額は22560円です。")

    async def test_assistant_caption_restores_official_moriguchi_spelling(self):
        display = FakeDisplay()
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio(), display)

        session._log_transcript(
            {
                "type": "session.output_transcript.delta",
                "delta": "もりぐち市役所、保険年金課です。",
            }
        )
        session._flush_all_transcripts()

        self.assertEqual(display.events[0]["text"], "守口市役所、保険年金課です。")
        self.assertEqual(display.events[-1]["text"], "守口市役所、保険年金課です。")

    async def test_archives_only_final_normalized_transcript(self):
        archive = FakeArchive()
        session = GPTLiveSession(
            Settings(api_key="test-key"), DummyAudio(), archive=archive
        )

        session._log_transcript(
            {
                "type": "session.output_transcript.delta",
                "delta": "にまんにせんごひゃくろくじゅうえんです。",
            }
        )
        self.assertEqual(archive.transcripts, [])
        session._flush_all_transcripts()

        self.assertEqual(archive.transcripts, [("assistant", "22560円です。")])

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
        self.assertIn("GPT-Liveセッションを開始しました", "\n".join(logs.output))
        self.assertIn("最初の「もしもし？」を先行準備", logs.output[-1])
        self.assertEqual(websocket.sent[-1], session._greeting_event())
        self.assertTrue(session._prepared)

    async def test_tracks_greeting_generated_during_ring_before_unmute_ack(self):
        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        session._mark_greeting_requested("initial_greeting")
        session.websocket = FakeWebSocket(
            [
                {
                    "type": "session.instructions.appended",
                    "client_event_id": "initial_greeting",
                },
                {
                    "type": "session.output_audio.delta",
                    "delta": base64.b64encode(b"prefetched greeting").decode("ascii"),
                },
                {"type": "session.output_transcript.delta", "delta": "もしもし？"},
                {
                    "type": "session.input_audio.unmuted",
                    "client_event_id": "handset_up_unmute",
                },
            ]
        )

        await session._wait_for_ack(
            "session.input_audio.unmuted", "handset_up_unmute"
        )

        self.assertTrue(session._greeting_instruction_accepted)
        self.assertTrue(session._greeting_audio_received)
        self.assertEqual(session._greeting_transcript, "もしもし？")
        self.assertEqual(audio.played, [b"prefetched greeting"])

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
                    "type": "session.instructions.appended",
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

    async def test_accepts_audio_control_ack_without_optional_client_event_id(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())
        session.websocket = FakeWebSocket(
            [{"type": "session.input_audio.unmuted"}]
        )

        await session._wait_for_ack(
            "session.input_audio.unmuted", "handset_up_unmute"
        )

    async def test_audio_control_ack_has_absolute_timeout_despite_other_events(self):
        class NoisyWebSocket(FakeWebSocket):
            async def recv(self):
                await asyncio.sleep(0)
                return json.dumps({"type": "session.info"})

        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())
        session.CONTROL_ACK_TIMEOUT_SECONDS = 0.01
        session.websocket = NoisyWebSocket()

        with self.assertRaisesRegex(RuntimeError, "0.01秒以内"):
            await asyncio.wait_for(
                session._wait_for_ack(
                    "session.input_audio.unmuted", "handset_up_unmute"
                ),
                timeout=0.2,
            )

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
                    "type": "session.instructions.appended",
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

    async def test_accepts_greeting_ack_without_optional_client_event_id(self):
        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        session.websocket = FakeWebSocket(
            [
                {"type": "session.instructions.appended"},
                {
                    "type": "session.output_audio.delta",
                    "delta": base64.b64encode(b"moshimoshi").decode("ascii"),
                },
                {"type": "session.output_transcript.delta", "delta": "もしもし"},
            ]
        )

        await session._wait_until_greeting_accepted()

        self.assertEqual(audio.played, [b"moshimoshi"])

    async def test_retries_greeting_once_when_no_speech_arrives(self):
        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        session.websocket = FakeWebSocket(
            [
                {
                    "type": "session.instructions.appended",
                    "client_event_id": "initial_greeting",
                },
                asyncio.TimeoutError(),
                {
                    "type": "session.instructions.appended",
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

    async def test_greeting_has_absolute_timeout_despite_other_events(self):
        class NoisyWebSocket(FakeWebSocket):
            async def recv(self):
                await asyncio.sleep(0)
                return json.dumps({"type": "session.info"})

        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())
        session.GREETING_TIMEOUT_SECONDS = 0.01
        session.websocket = NoisyWebSocket()

        with self.assertRaisesRegex(RuntimeError, "初回挨拶"):
            await asyncio.wait_for(
                session._wait_until_greeting_accepted(), timeout=0.2
            )

        self.assertEqual(
            session.websocket.sent,
            [session._greeting_event("initial_greeting_retry")],
        )

    async def test_starts_after_retry_when_greeting_audio_has_no_transcript(self):
        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        session.websocket = FakeWebSocket(
            [
                {
                    "type": "session.instructions.appended",
                    "client_event_id": "initial_greeting",
                },
                {
                    "type": "session.output_audio.delta",
                    "delta": base64.b64encode(b"moshimoshi").decode("ascii"),
                },
                asyncio.TimeoutError(),
                asyncio.TimeoutError(),
            ]
        )

        with self.assertLogs("kuro_sagi_denwa.live", level="WARNING") as logs:
            await session._wait_until_greeting_accepted()

        self.assertEqual(audio.played, [b"moshimoshi"])
        self.assertEqual(audio.wait_count, 1)
        self.assertEqual(
            session.websocket.sent,
            [session._greeting_event("initial_greeting_retry")],
        )
        self.assertIn("字幕は届きませんでした", logs.output[-1])

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

    async def test_sends_first_microphone_packet_before_greeting(self):
        audio = DummyAudio()
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        session.websocket = FakeWebSocket()
        await audio.input_queue.put(b"first pcm")

        await session._send_first_audio_packet()

        self.assertEqual(session.websocket.sent[0]["type"], "session.input_audio.append")
        self.assertEqual(
            session.websocket.sent[0]["audio"],
            base64.b64encode(b"first pcm").decode("ascii"),
        )
        await asyncio.wait_for(audio.input_queue.join(), timeout=0.1)

    async def test_first_microphone_packet_has_timeout(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())
        session.websocket = FakeWebSocket()
        session.FIRST_AUDIO_TIMEOUT_SECONDS = 0.01

        with self.assertRaisesRegex(RuntimeError, "最初の音声データ"):
            await session._send_first_audio_packet()

    async def test_dial_is_ignored(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())
        session.websocket = FakeWebSocket()

        await session.notify_dial(7)

        self.assertEqual(session.websocket.sent, [])


if __name__ == "__main__":
    unittest.main()
