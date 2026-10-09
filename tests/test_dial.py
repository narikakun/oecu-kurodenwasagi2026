import unittest

from kuro_sagi_denwa.dial import DialDecoder


class DialDecoderTest(unittest.TestCase):
    def test_one_to_nine_are_returned_as_is(self):
        for expected in range(1, 10):
            decoder = DialDecoder(digit_timeout_seconds=0.15)
            for index in range(expected):
                decoder.add_pulse(index * 0.05)

            self.assertEqual(
                decoder.read_digit((expected - 1) * 0.05 + 0.16), expected
            )

    def test_ten_pulses_become_zero(self):
        decoder = DialDecoder()
        for index in range(10):
            decoder.add_pulse(index * 0.05)

        self.assertEqual(decoder.read_digit(0.61), 0)

    def test_pulse_shorter_than_dial_period_is_ignored(self):
        # 20パルス/秒の1パルス（約50ms）の途中に来た余分なパルスは数えない。
        decoder = DialDecoder(min_pulse_interval_seconds=0.025)
        decoder.add_pulse(1.000)
        decoder.add_pulse(1.012)
        decoder.add_pulse(1.050)
        self.assertEqual(decoder.pulse_count, 2)

    def test_single_pulse_with_chatter_reads_as_one(self):
        decoder = DialDecoder()
        decoder.add_pulse(1.000)
        decoder.add_pulse(1.010)
        self.assertEqual(decoder.read_digit(1.200), 1)
        self.assertIsNone(decoder.read_digit(1.200))

    def test_late_pulses_are_split_into_digits_by_timestamp(self):
        # 取り出しが遅れて2桁分のパルスがまとめて届いても合算しない。
        decoder = DialDecoder(digit_timeout_seconds=0.15)
        for index in range(3):
            decoder.add_pulse(1.0 + index * 0.05)
        for index in range(2):
            decoder.add_pulse(1.5 + index * 0.05)

        self.assertEqual(decoder.read_digit(2.0), 3)
        self.assertEqual(decoder.read_digit(2.0), 2)
        self.assertIsNone(decoder.read_digit(2.0))

    def test_edge_timestamps_override_delayed_callbacks(self):
        # コールバックが遅れて2回分が8ms差で届いても、エッジが50ms間隔なら2パルス。
        decoder = DialDecoder(min_pulse_interval_seconds=0.025)
        decoder.add_pulse(1.200, edge_at=0.000)
        decoder.add_pulse(1.208, edge_at=0.050)
        self.assertEqual(decoder.read_digit(1.400), 2)

    def test_edge_timestamps_keep_stretched_callbacks_in_one_digit(self):
        # コールバック同士が150ms以上空いても、エッジが50ms間隔なら同じ桁。
        decoder = DialDecoder(digit_timeout_seconds=0.15)
        decoder.add_pulse(1.000, edge_at=0.000)
        decoder.add_pulse(1.140, edge_at=0.050)
        decoder.add_pulse(1.160, edge_at=0.100)
        self.assertEqual(decoder.read_digit(1.400), 3)

    def test_first_pulse_of_each_operation_is_skipped(self):
        decoder = DialDecoder(skip_first_pulse=True)
        # 回し始めの余分なパルス → 戻りで本物の2パルス
        decoder.add_pulse(1.000)
        self.assertIsNone(decoder.read_digit(1.300))
        decoder.add_pulse(1.500)
        decoder.add_pulse(1.555)
        self.assertEqual(decoder.read_digit(1.800), 2)

        # 次の操作でも同じく最初の1パルスを捨てる。
        decoder.add_pulse(3.000)
        decoder.add_pulse(3.600)
        self.assertEqual(decoder.read_digit(3.800), 1)

    def test_skipped_pulse_merged_with_digit_is_removed(self):
        # 素早く離して余分なパルスが本物のパルスと続けて届いても1つだけ捨てる。
        decoder = DialDecoder(skip_first_pulse=True)
        for index in range(3):
            decoder.add_pulse(1.0 + index * 0.055)
        self.assertEqual(decoder.read_digit(1.5), 2)

    def test_chatter_right_after_skipped_pulse_is_ignored(self):
        decoder = DialDecoder(skip_first_pulse=True)
        decoder.add_pulse(1.000)
        decoder.add_pulse(1.010)
        decoder.add_pulse(1.500)
        self.assertEqual(decoder.read_digit(1.700), 1)

    def test_lone_skipped_pulse_times_out(self):
        decoder = DialDecoder(skip_first_pulse=True, first_pulse_timeout_seconds=3.0)
        decoder.add_pulse(1.000)
        with self.assertLogs("kuro_sagi_denwa.dial", level="WARNING"):
            self.assertIsNone(decoder.read_digit(4.100))
        # 待機状態へ戻ったので、次のパルスは再び回し始めとして捨てる。
        decoder.add_pulse(5.000)
        decoder.add_pulse(5.500)
        self.assertEqual(decoder.read_digit(5.700), 1)

    def test_backlogged_operations_skip_each_first_pulse(self):
        # 取り出しが遅れて2操作分がまとめて届いても、各操作の先頭を捨てる。
        decoder = DialDecoder(skip_first_pulse=True)
        decoder.add_pulse(1.000)
        decoder.add_pulse(1.500)
        decoder.add_pulse(1.555)
        decoder.add_pulse(2.500)
        decoder.add_pulse(3.000)
        self.assertEqual(decoder.read_digit(3.300), 2)
        self.assertEqual(decoder.read_digit(3.300), 1)

    def test_invalid_pulse_count_is_logged(self):
        decoder = DialDecoder()
        for index in range(11):
            decoder.add_pulse(index * 0.05)

        with self.assertLogs("kuro_sagi_denwa.dial", level="WARNING") as logs:
            self.assertIsNone(decoder.read_digit(1.0))
        self.assertIn("11", logs.output[0])

    def test_reset_discards_ready_digits(self):
        decoder = DialDecoder()
        decoder.add_pulse(1.0)
        decoder.add_pulse(2.0)
        decoder.reset()
        self.assertIsNone(decoder.read_digit(3.0))

    def test_digit_is_not_ready_before_timeout(self):
        decoder = DialDecoder(digit_timeout_seconds=0.15)
        decoder.add_pulse(1.0)
        self.assertIsNone(decoder.read_digit(1.149))
        self.assertEqual(decoder.read_digit(1.151), 1)

    def test_invalid_pulse_count_is_discarded(self):
        decoder = DialDecoder(digit_timeout_seconds=0.15)
        for index in range(11):
            decoder.add_pulse(index * 0.05)

        self.assertIsNone(decoder.read_digit(0.66))
        self.assertEqual(decoder.pulse_count, 0)


if __name__ == "__main__":
    unittest.main()
