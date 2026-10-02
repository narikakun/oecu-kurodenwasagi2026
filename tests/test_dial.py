import unittest

from kuro_sagi_denwa.dial import DialDecoder


class DialDecoderTest(unittest.TestCase):
    def test_one_to_nine_are_returned_as_is(self):
        for expected in range(1, 10):
            decoder = DialDecoder(debounce_seconds=0.01, digit_timeout_seconds=0.15)
            for index in range(expected):
                self.assertTrue(decoder.add_pulse(index * 0.05))

            self.assertEqual(
                decoder.read_digit((expected - 1) * 0.05 + 0.16), expected
            )

    def test_ten_pulses_become_zero(self):
        decoder = DialDecoder()
        for index in range(10):
            decoder.add_pulse(index * 0.05)

        self.assertEqual(decoder.read_digit(0.61), 0)

    def test_chattering_pulse_is_ignored(self):
        decoder = DialDecoder(debounce_seconds=0.012)
        self.assertTrue(decoder.add_pulse(1.000))
        self.assertFalse(decoder.add_pulse(1.005))
        self.assertTrue(decoder.add_pulse(1.050))
        self.assertEqual(decoder.pulse_count, 2)

    def test_digit_is_not_ready_before_timeout(self):
        decoder = DialDecoder(digit_timeout_seconds=0.15)
        decoder.add_pulse(1.0)
        self.assertIsNone(decoder.read_digit(1.149))
        self.assertEqual(decoder.read_digit(1.151), 1)

    def test_invalid_pulse_count_is_discarded(self):
        decoder = DialDecoder(debounce_seconds=0.01, digit_timeout_seconds=0.15)
        for index in range(11):
            self.assertTrue(decoder.add_pulse(index * 0.05))

        self.assertIsNone(decoder.read_digit(0.66))
        self.assertEqual(decoder.pulse_count, 0)


if __name__ == "__main__":
    unittest.main()
