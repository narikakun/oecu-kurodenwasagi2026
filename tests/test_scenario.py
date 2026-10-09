import unittest

from kuro_sagi_denwa.scenario import REFUND_FRAUD_SCENARIO


class RefundFraudScenarioTest(unittest.TestCase):
    def test_uses_fictional_refund_scam_details(self):
        self.assertIn("朝凪市役所", REFUND_FRAUD_SCENARIO)
        self.assertIn("保険年金課", REFUND_FRAUD_SCENARIO)
        self.assertIn("佐藤", REFUND_FRAUD_SCENARIO)
        self.assertIn("高橋様", REFUND_FRAUD_SCENARIO)
        self.assertIn("22,560円", REFUND_FRAUD_SCENARIO)

    def test_asks_for_unknown_context_without_collecting_identity(self):
        self.assertIn("本当の名前を聞かず", REFUND_FRAUD_SCENARIO)
        self.assertIn("ご家族の方でしょうか", REFUND_FRAUD_SCENARIO)
        self.assertIn("具体的な所在地は聞かない", REFUND_FRAUD_SCENARIO)

    def test_supports_natural_safe_and_risk_routes(self):
        self.assertIn("選択肢、クイズ", REFUND_FRAUD_SCENARIO)
        self.assertIn("安全行動の扱い", REFUND_FRAUD_SCENARIO)
        self.assertIn("危険行動の扱い", REFUND_FRAUD_SCENARIO)
        self.assertIn("途中まで従ったことを責めず", REFUND_FRAUD_SCENARIO)

    def test_does_not_provide_actionable_transfer_steps(self):
        self.assertIn("ATMの具体的なボタン順", REFUND_FRAUD_SCENARIO)
        self.assertIn("具体的な送金操作へは進まない", REFUND_FRAUD_SCENARIO)


if __name__ == "__main__":
    unittest.main()
