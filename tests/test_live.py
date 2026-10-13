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
        self.played_files = []
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

    async def play_audio_file(self, path, volume=1.0):
        self.played_files.append((path, volume))

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

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()


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

    def test_restores_spoken_symbols_and_initialism_for_captions(self):
        spoken = "エーティーエムではなく、警察相談専用電話はシャープ９１１０です。"
        expected = "ATMではなく、警察相談専用電話は#9110です。"

        self.assertEqual(
            normalize_transcript_for_display(spoken, assistant=True), expected
        )
        self.assertEqual(
            normalize_transcript_for_display(spoken, assistant=False), expected
        )

    def test_normalizes_common_spelling_variants_in_captions(self):
        self.assertEqual(
            normalize_transcript_for_display(
                "エー・ティー・エム、えーてぃーえむ、ＡＴＭ、しゃーぷ188",
                assistant=True,
            ),
            "ATM、ATM、ATM、#188",
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
        self.assertIn(
            "受話器越しの相手があなたに話しかけたときだけ",
            event["session"]["instructions"],
        )
        self.assertIn(
            "あなたへ向けられていない音声は割り込みとして扱わず",
            event["session"]["instructions"],
        )
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

    def test_recorded_greeting_is_24khz_mono_pcm16(self):
        session = GPTLiveSession(Settings(api_key="test-key"), DummyAudio())

        pcm = session._load_recorded_greeting()

        self.assertGreater(len(pcm), 0)
        self.assertEqual(len(pcm) % 2, 0)


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

    async def test_recorded_greeting_is_displayed_and_archived(self):
        audio = DummyAudio()
        display = FakeDisplay()
        archive = FakeArchive()
        session = GPTLiveSession(
            Settings(api_key="test-key"), audio, display, archive
        )

        await session._play_recorded_greeting()

        self.assertEqual(audio.wait_count, 1)
        self.assertEqual(archive.transcripts, [("assistant", "もしもし？")])
        self.assertEqual(
            display.events,
            [
                {
                    "type": "transcript",
                    "speaker": "assistant",
                    "status": "final",
                    "text": "もしもし？",
                }
            ],
        )

    async def test_disconnect_sound_plays_once_before_feedback_resumes(self):
        audio = DummyAudio()
        display = FakeDisplay()
        session = GPTLiveSession(Settings(api_key="test-key"), audio, display)
        session.websocket = FakeWebSocket()

        session._log_transcript(
            {
                "type": "session.output_transcript.delta",
                "delta": "承知しました。それでは失礼します",
            }
        )
        self.assertIsNotNone(session._feedback_transition_task)
        await session._feedback_transition_task

        self.assertEqual(
            audio.played_files,
            [(session.DISCONNECT_AUDIO_PATH, 1.0)],
        )
        self.assertEqual(display.events[0]["type"], "transcript")
        self.assertEqual(
            display.events[-1],
            {
                "type": "feedback",
                "active": True,
                "image": "refund-fraud-warning.png",
                "alt": "還付金詐欺にご用心！",
            },
        )
        self.assertEqual(
            session.websocket.sent,
            [
                {
                    "type": "session.instructions.append",
                    "event_id": "resume_feedback_after_disconnect",
                    "delegation_id": None,
                    "content": session.FEEDBACK_RESUME_INSTRUCTION,
                }
            ],
        )

        session._log_transcript(
            {"type": "session.output_transcript.delta", "delta": "。それでは失礼します"}
        )
        self.assertEqual(len(audio.played_files), 1)

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
        websocket = FakeWebSocket([{"type": "session.started"}])

        fake_websockets = SimpleNamespace(
            connect=AsyncMock(return_value=websocket)
        )
        with patch.dict(sys.modules, {"websockets": fake_websockets}):
            with self.assertLogs("kuro_sagi_denwa.live", level="INFO") as logs:
                await session.prepare()
            await session.prepare()

        self.assertIn("GPT-Liveセッションへ接続します", logs.output[0])
        self.assertIn("GPT-Liveセッションを開始しました", "\n".join(logs.output))
        self.assertNotIn("もしもし", "\n".join(logs.output))
        self.assertFalse(
            any(event["type"] == "session.instructions.append" for event in websocket.sent)
        )
        fake_websockets.connect.assert_awaited_once()
        self.assertFalse(
            any(event["type"] == "session.input_audio.mute" for event in websocket.sent)
        )
        self.assertTrue(session._prepared)

    async def test_plays_recorded_greeting_before_first_microphone_audio(self):
        audio = DummyAudio()
        await audio.input_queue.put(b"first pcm")
        session = GPTLiveSession(Settings(api_key="test-key"), audio)
        session.websocket = FakeWebSocket()
        session._prepared = True

        await session.start()

        self.assertEqual(audio.played, [session._load_recorded_greeting()])
        self.assertEqual(audio.wait_count, 1)
        self.assertEqual(session.websocket.sent[0]["type"], "session.input_audio.append")
        self.assertFalse(
            any(
                event["type"] == "session.instructions.append"
                for event in session.websocket.sent
            )
        )
        self.assertTrue(session.is_conversation_active)
        await session._abort_start()

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
