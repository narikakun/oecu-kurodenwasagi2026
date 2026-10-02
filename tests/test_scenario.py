import unittest

from kuro_sagi_denwa.scenario import FraudScenario


class FraudScenarioTest(unittest.TestCase):
    def test_greeting_only_says_hello_and_waits(self):
        scenario = FraudScenario(ticket_number="547")
        instruction = scenario.greeting_instruction()

        self.assertIn("もしもし。", instruction)
        self.assertIn("相手の返答を待って", instruction)
        self.assertNotIn("通信サービスの確認担当です", instruction)

        service_prompt = scenario.build_instructions(role="service")
        self.assertIn("最初の発話として必ず「もしもし。」とだけ", service_prompt)

    def test_ticket_number_and_expected_digit(self):
        scenario = FraudScenario(ticket_number="547")

        self.assertEqual(scenario.expected_digit, 7)
        self.assertIn("五、四、七", scenario.build_instructions(role="police"))
        self.assertNotIn("五、四、七", scenario.build_instructions(role="service"))

    def test_correct_dial_advances_to_final_decision(self):
        scenario = FraudScenario(ticket_number="547")
        instruction = scenario.dial_instruction(7)

        self.assertIn("一致します", instruction)
        self.assertIn("最終判断", instruction)

    def test_wrong_dial_is_repeated_only_once(self):
        scenario = FraudScenario(ticket_number="547")
        instruction = scenario.dial_instruction(3)

        self.assertIn("一致しません", instruction)
        self.assertIn("一度だけ", instruction)
        self.assertIn("7", instruction)

    def test_prompt_contains_safety_rules(self):
        prompt = FraudScenario(ticket_number="547").build_instructions()

        self.assertIn("本名、生年月日、住所、電話番号", prompt)
        self.assertIn("具体的な送金操作へ進まない", prompt)
        self.assertIn("それでも断られたら終了方向へ", prompt)

    def test_non_contract_holder_is_asked_relationship_and_call_continues(self):
        prompt = FraudScenario(ticket_number="547").build_instructions()

        self.assertIn("どういったご関係ですか", prompt)
        self.assertIn("では、代わりに確認をお願いします", prompt)
        self.assertIn("関係を聞き直さず", prompt)

    def test_ai_uses_fixed_hangup_phrase_only_at_end(self):
        prompt = FraudScenario(ticket_number="547").build_instructions(role="police")

        self.assertIn("それでは、失礼します。", prompt)
        self.assertIn("この終了文は途中で使わず", prompt)

    def test_prompt_prevents_waiting_loop_and_handles_scam_suspicion(self):
        prompt = FraudScenario(ticket_number="547").build_instructions(role="police")

        self.assertIn("整理します", prompt)
        self.assertIn("少々お待ちください", prompt)
        self.assertIn("詐欺を疑われたら", prompt)
        self.assertIn("このまま確認を続けてもよろしいですか", prompt)

    def test_transfer_stops_service_session_and_police_has_own_greeting(self):
        scenario = FraudScenario(ticket_number="547")
        service_prompt = scenario.build_instructions(role="service")
        police_prompt = scenario.build_instructions(role="police")

        self.assertIn("別セッションへ切り替わる", service_prompt)
        self.assertIn("警察担当は演じません", service_prompt)
        self.assertIn("入力履歴は通信担当からの引き継ぎ", police_prompt)
        self.assertIn("お電話代わりました", scenario.police_greeting_instruction())

    def test_each_role_receives_only_its_own_workflow(self):
        scenario = FraudScenario(ticket_number="547")
        service_prompt = scenario.build_instructions(role="service")
        police_prompt = scenario.build_instructions(role="police")

        self.assertNotIn("ダイヤル確認", service_prompt)
        self.assertNotIn("安全確認用の口座", service_prompt)
        self.assertNotIn("それでは、失礼します", service_prompt)
        self.assertNotIn("通信サービスの確認担当です", police_prompt)
        self.assertNotIn("担当へおつなぎします", police_prompt)
        self.assertIn("ダイヤル確認", police_prompt)
        self.assertIn("安全確認用の口座", police_prompt)
        self.assertIn("確認し直さないでください", police_prompt)
        self.assertNotIn("身に覚えのない契約があるとお話しされましたね", police_prompt)

    def test_prompt_uses_answers_instead_of_forcing_script_order(self):
        prompt = FraudScenario(ticket_number="547").build_instructions()

        self.assertIn("答え済みの質問は飛ばして", prompt)
        self.assertIn("母が契約者です", prompt)
        self.assertIn("あるかも", prompt)
        self.assertIn("心当たりがあるのですね", prompt)
        self.assertIn("覚えがない」と言ってはいけません", prompt)
        self.assertIn("空疎な相づち", prompt)
        self.assertIn("情報が漏れて名義を不正利用された可能性があります", prompt)
        self.assertIn("情報漏洩を事実と断定しません", prompt)

    def test_reflection_instructions_forbid_waiting(self):
        scenario = FraudScenario(ticket_number="547")

        self.assertIn("安全行動", scenario.safe_reflection_instruction())
        self.assertIn("待つ", scenario.safe_reflection_instruction())
        self.assertIn("詐欺でよく使われる誘導", scenario.danger_reflection_instruction())
        self.assertIn("手続きを続けることは禁止", scenario.danger_reflection_instruction())

    def test_police_recovery_uses_current_stage_and_handoff(self):
        scenario = FraudScenario(ticket_number="547")

        instruction = scenario.police_next_instruction(2)

        self.assertIn("引き継いだ履歴", instruction)
        self.assertIn("用件を説明し直させない", instruction)
        self.assertIn("知らない人に電話を貸した", instruction)


if __name__ == "__main__":
    unittest.main()
