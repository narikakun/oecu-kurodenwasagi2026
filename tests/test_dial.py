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
