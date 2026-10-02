import unittest

from kuro_sagi_denwa.scenario import FraudScenario


class FraudScenarioTest(unittest.TestCase):
    def test_greeting_only_says_hello_and_waits(self):
        scenario = FraudScenario(ticket_number="547")
        instruction = scenario.greeting_instruction()

        self.assertIn("もしもし。", instruction)
        self.assertIn("相手の返答を待って", instruction)
        self.assertNotIn("通信サービスの確認担当です", instruction)

    def test_ticket_number_and_expected_digit(self):
        scenario = FraudScenario(ticket_number="547")

        self.assertEqual(scenario.expected_digit, 7)
        self.assertIn("五、四、七", scenario.build_instructions())

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

        self.assertIn("本名、生年月日、住所、電話番号を尋ねない", prompt)
        self.assertIn("具体的な操作へ進まない", prompt)
        self.assertIn("それでも断られたら引き止めをやめ", prompt)

    def test_non_contract_holder_is_asked_relationship_and_call_continues(self):
        prompt = FraudScenario(ticket_number="547").build_instructions()

        self.assertIn("どういったご関係ですか", prompt)
        self.assertIn("では、代わりに確認をお願いします", prompt)
        self.assertIn("関係性が何であっても", prompt)

    def test_ai_uses_fixed_hangup_phrase_only_at_end(self):
        prompt = FraudScenario(ticket_number="547").build_instructions()

        self.assertIn("それでは、失礼します。", prompt)
        self.assertIn("会話の途中では絶対に使わず", prompt)

    def test_prompt_prevents_waiting_loop_and_handles_scam_suspicion(self):
        prompt = FraudScenario(ticket_number="547").build_instructions()

        self.assertIn("整理します", prompt)
        self.assertIn("少々お待ちください", prompt)
        self.assertIn("詐欺ではないですか", prompt)
        self.assertIn("このまま確認を続けてもよろしいですか", prompt)

    def test_transfer_stops_service_session_and_police_has_own_greeting(self):
        scenario = FraudScenario(ticket_number="547")
        service_prompt = scenario.build_instructions(role="service")
        police_prompt = scenario.build_instructions(role="police")

        self.assertIn("別のセッションへ切り替える", service_prompt)
        self.assertIn("そこで必ず発話を止めて", service_prompt)
        self.assertIn("警察担当の段階から続けて", police_prompt)
        self.assertIn("お電話代わりました", scenario.police_greeting_instruction())

    def test_prompt_uses_answers_instead_of_forcing_script_order(self):
        prompt = FraudScenario(ticket_number="547").build_instructions()

        self.assertIn("答え済みの質問を飛ばして", prompt)
        self.assertIn("母が契約者です", prompt)
        self.assertIn("あるかも", prompt)
        self.assertIn("心当たりがあるのですね", prompt)
        self.assertIn("覚えがない」と決めつけない", prompt)
        self.assertIn("そうお感じなのですね", prompt)
        self.assertIn("情報が漏れて名義を不正利用された可能性があります", prompt)
        self.assertIn("事実確認済みの情報漏洩として断定してはいけません", prompt)


if __name__ == "__main__":
    unittest.main()
