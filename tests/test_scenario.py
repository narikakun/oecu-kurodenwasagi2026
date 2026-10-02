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


if __name__ == "__main__":
    unittest.main()
