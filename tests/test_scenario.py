import unittest
from datetime import date

from kuro_sagi_denwa.scenario import build_refund_fraud_scenario


class RefundFraudScenarioTest(unittest.TestCase):
    def setUp(self):
        self.scenario = build_refund_fraud_scenario(date(2026, 10, 9))

    def test_uses_fictional_refund_scam_details(self):
        self.assertIn("守口市役所", self.scenario)
        self.assertIn(
            "突然のお電話で失礼します。もりぐち市役所、保険年金課の佐藤と申します。",
            self.scenario,
        )
        self.assertIn("「まもりくち市役所」とは絶対に言わない", self.scenario)
        self.assertNotIn("朝凪市役所", self.scenario)
        self.assertIn("保険年金課", self.scenario)
        self.assertIn("佐藤", self.scenario)
        self.assertIn(
            "お電話口の方に、そのままご説明してもよろしいでしょうか",
            self.scenario,
        )
        self.assertNotIn("ご本人様でよろしいでしょうか", self.scenario)
        self.assertNotIn("高橋", self.scenario)
        self.assertIn("22,560円", self.scenario)

    def test_builds_dates_from_call_date(self):
        self.assertIn("書類送付日: 7月9日", self.scenario)
        self.assertIn("申請期限: 10月8日", self.scenario)
        self.assertNotIn("6月13日", self.scenario)
        self.assertNotIn("9月13日", self.scenario)

    def test_rounds_notice_date_to_end_of_month(self):
        scenario = build_refund_fraud_scenario(date(2026, 5, 31))
        self.assertIn("書類送付日: 2月28日", scenario)
        self.assertIn("申請期限: 5月30日", scenario)

    def test_asks_for_unknown_context_without_collecting_identity(self):
        self.assertIn("本当の名前を聞かず", self.scenario)
        self.assertIn("ご家族の方でしょうか", self.scenario)
        self.assertIn("具体的な所在地は聞かない", self.scenario)

    def test_supports_natural_safe_and_risk_routes(self):
        self.assertIn("選択肢、クイズ", self.scenario)
        self.assertIn("安全行動の扱い", self.scenario)
        self.assertIn("危険行動の扱い", self.scenario)
        self.assertIn("途中まで従ったことを責めず", self.scenario)

    def test_waits_for_disconnect_sound_before_feedback(self):
        self.assertIn("犯人役から振り返りへ移る共通手順", self.scenario)
        self.assertIn("「それでは失礼します」とだけ言う", self.scenario)
        self.assertIn("通話終了音の再生完了", self.scenario)
        self.assertIn("振り返りや次の言葉を一切話さない", self.scenario)

    def test_does_not_provide_actionable_transfer_steps(self):
        self.assertIn("ATMの具体的なボタン順", self.scenario)
        self.assertIn("具体的な送金操作へは進まない", self.scenario)


if __name__ == "__main__":
    unittest.main()
