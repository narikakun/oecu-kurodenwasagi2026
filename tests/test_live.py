import asyncio
import unittest
from unittest.mock import AsyncMock

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
        self.assertNotIn("五、四、七", event["session"]["instructions"])
        self.assertNotIn("ダイヤル確認", event["session"]["instructions"])
        self.assertEqual(event["session"]["input"], [])

    def test_police_session_uses_another_voice_and_saved_history(self):
        settings = Settings(api_key="test-key", police_voice="cedar")
        session = GPTLiveSession(
            settings, DummyAudio(), FraudScenario(ticket_number="547")
        )
        session._remember_transcript("user", "はい、お願いします")

        event = session._session_start_event("police", settings.police_voice)

        self.assertEqual(event["session"]["audio"]["output"]["voice"], "cedar")
        self.assertEqual(event["session"]["input"][0]["role"], "developer")
        self.assertIn(
            "引き継いだ直前の通話記録",
            event["session"]["input"][0]["content"][0]["text"],
        )
        self.assertEqual(event["session"]["input"][1]["role"], "user")
        self.assertEqual(
            event["session"]["input"][1]["content"][0]["type"], "input_text"
        )
        self.assertIn("入力履歴は通信担当からの引き継ぎ", event["session"]["instructions"])
        self.assertIn("五、四、七", event["session"]["instructions"])

    def test_event_ids_are_unique(self):
        settings = Settings(api_key="test-key")
        session = GPTLiveSession(
            settings, DummyAudio(), FraudScenario(ticket_number="547")
        )

        self.assertNotEqual(
            session._next_event_id("dial_7"), session._next_event_id("dial_7")
        )

    def test_hangup_phrase_can_be_detected_across_fragments(self):
        settings = Settings(api_key="test-key")
        session = GPTLiveSession(
            settings, DummyAudio(), FraudScenario(ticket_number="547")
        )

        self.assertFalse(session._detect_ai_hangup("それでは、"))
        self.assertTrue(session._detect_ai_hangup("失礼します。"))

    def test_handoff_phrase_can_be_detected_across_fragments(self):
        settings = Settings(api_key="test-key")
        session = GPTLiveSession(
            settings, DummyAudio(), FraudScenario(ticket_number="547")
        )

        self.assertFalse(session._detect_handoff("担当へおつなぎ"))
        self.assertTrue(session._detect_handoff("します。"))

    def test_audible_pcm_ignores_silence(self):
        self.assertFalse(GPTLiveSession._is_audible_pcm(bytes(960)))
        self.assertTrue(
            GPTLiveSession._is_audible_pcm((500).to_bytes(2, "little", signed=True))
        )

    def test_conversation_test_mode_has_no_fraud_scenario(self):
        settings = Settings(
            api_key="test-key",
            conversation_test_mode=True,
            live_instructions="明るい声で話してください。",
        )
        session = GPTLiveSession(settings, DummyAudio())

        event = session._session_start_event("test")
        instructions = event["session"]["instructions"]

        self.assertIn("自由に会話", instructions)
        self.assertIn("明るい声", instructions)
        self.assertNotIn("安全確認用の口座", instructions)
        self.assertNotIn("担当へおつなぎします", instructions)
        self.assertEqual(event["session"]["input"], [])

    def test_test_mode_does_not_detect_scenario_phrases(self):
        session = GPTLiveSession(
            Settings(api_key="test-key", conversation_test_mode=True), DummyAudio()
        )

        self.assertFalse(session._detect_handoff("担当へおつなぎします。"))
        self.assertFalse(session._detect_ai_hangup("それでは、失礼します。"))


class GPTLiveSilenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_first_hello_is_requested_again(self):
        session = GPTLiveSession(
            Settings(api_key="test-key", turn_reply_timeout_ms=1),
            DummyAudio(),
            FraudScenario(ticket_number="547"),
        )
        session.append_instructions = AsyncMock()
        session._waiting_for_service_greeting_audio = True

        await session._ensure_service_greeting()

        self.assertEqual(session.append_instructions.await_count, 2)
        content = session.append_instructions.await_args_list[0].args[0]
        self.assertIn("今すぐ日本語で「もしもし。」", content)

    async def test_dial_is_ignored_until_ai_requests_it(self):
        session = GPTLiveSession(
            Settings(api_key="test-key"),
            DummyAudio(),
            FraudScenario(ticket_number="547"),
        )
        session.append_instructions = AsyncMock()

        await session.notify_dial(9)

        session.append_instructions.assert_not_awaited()

    async def test_dial_is_accepted_only_during_dial_stage(self):
        session = GPTLiveSession(
            Settings(api_key="test-key"),
            DummyAudio(),
            FraudScenario(ticket_number="547"),
        )
        session.append_instructions = AsyncMock()
        session._update_conversation_state("末尾の七をダイヤルで")
        session._update_conversation_state("回してください。")

        await session.notify_dial(7)

        session.append_instructions.assert_awaited_once()
        self.assertFalse(session._dial_enabled)

    async def test_police_reply_watchdog_uses_current_stage(self):
        session = GPTLiveSession(
            Settings(api_key="test-key", turn_reply_timeout_ms=1),
            DummyAudio(),
            FraudScenario(ticket_number="547"),
        )
        session.append_instructions = AsyncMock()
        session._role = "police"
        session._police_stage = 2

        session._start_reply_watchdog()
        await asyncio.sleep(0.003)

        content = session.append_instructions.await_args_list[0].args[0]
        self.assertIn("知らない人に電話を貸した", content)
        self.assertIn("用件を説明し直させない", content)
        session._cancel_reply_watchdog()

    async def test_completed_handoff_does_not_block_police_reply(self):
        session = GPTLiveSession(
            Settings(api_key="test-key", turn_reply_timeout_ms=1),
            DummyAudio(),
            FraudScenario(ticket_number="547"),
        )
        session.append_instructions = AsyncMock()
        session._role = "police"
        session._police_stage = 1
        session._handoff_task = asyncio.create_task(asyncio.sleep(0))
        await session._handoff_task

        session._remember_transcript("user", "はい、もしもし")
        await asyncio.sleep(0.003)

        self.assertGreaterEqual(session.append_instructions.await_count, 1)
        content = session.append_instructions.await_args_list[0].args[0]
        self.assertIn("知らない人に電話を貸した", content)
        self.assertNotIn("身に覚えのない契約があるとお話し", content)
        session._cancel_reply_watchdog()

    async def test_final_agreement_forces_danger_reflection(self):
        session = GPTLiveSession(
            Settings(api_key="test-key"),
            DummyAudio(),
            FraudScenario(ticket_number="547"),
        )
        session.append_instructions = AsyncMock()
        session._awaiting_final_decision = True

        await session._steer_after_user_reply("はい")

        content = session.append_instructions.await_args.args[0]
        self.assertIn("犯人役を終え", content)
        self.assertIn("詐欺でよく使われる誘導", content)

    async def test_family_consultation_forces_safe_reflection(self):
        session = GPTLiveSession(
            Settings(api_key="test-key"),
            DummyAudio(),
            FraudScenario(ticket_number="547"),
        )
        session.append_instructions = AsyncMock()

        await session._steer_after_user_reply("一回家族に相談します")

        content = session.append_instructions.await_args.args[0]
        self.assertIn("安全行動", content)
        self.assertIn("犯人役を終え", content)

    async def test_silence_timer_starts_after_greeting_transcript(self):
        settings = Settings(api_key="test-key", initial_silence_prompt_ms=60_000)
        session = GPTLiveSession(
            settings, DummyAudio(), FraudScenario(ticket_number="547")
        )

        self.assertIsNone(session._initial_silence_task)
        session._remember_transcript("assistant", "もしもし。")
        self.assertIsNotNone(session._initial_silence_task)

        session._initial_silence_task.cancel()
        await asyncio.gather(session._initial_silence_task, return_exceptions=True)

    async def test_initial_silence_sends_second_greeting(self):
        settings = Settings(api_key="test-key", initial_silence_prompt_ms=1)
        session = GPTLiveSession(
            settings, DummyAudio(), FraudScenario(ticket_number="547")
        )
        session.append_instructions = AsyncMock()

        await session._prompt_after_initial_silence()

        session.append_instructions.assert_awaited_once()
        content = session.append_instructions.await_args.args[0]
        self.assertIn("もしもし、聞こえますでしょうか", content)
        self.assertTrue(session._initial_retry_sent)

    async def test_reply_after_second_greeting_explicitly_resumes_scenario(self):
        settings = Settings(api_key="test-key")
        session = GPTLiveSession(
            settings, DummyAudio(), FraudScenario(ticket_number="547")
        )
        session.append_instructions = AsyncMock()
        session._initial_retry_sent = True

        session._remember_transcript("user", "はい、聞こえます")
        await asyncio.sleep(0)

        session.append_instructions.assert_awaited_once()
        content = session.append_instructions.await_args.args[0]
        self.assertIn("通信サービスの確認担当です", content)

    async def test_each_user_turn_gets_a_reply_watchdog(self):
        settings = Settings(api_key="test-key", turn_reply_timeout_ms=1)
        session = GPTLiveSession(
            settings, DummyAudio(), FraudScenario(ticket_number="547")
        )
        session.append_instructions = AsyncMock()

        session._remember_transcript("user", "はい")
        await asyncio.sleep(0.003)

        self.assertGreaterEqual(session.append_instructions.await_count, 1)
        first_content = session.append_instructions.await_args_list[0].args[0]
        self.assertIn("すでに答えた質問は飛ばして", first_content)
        session._cancel_reply_watchdog()


if __name__ == "__main__":
    unittest.main()
