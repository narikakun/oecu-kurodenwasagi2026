import unittest

from array import array

from kuro_sagi_denwa.audio import AudioDevice, resample_pcm16_mono
from kuro_sagi_denwa.config import Settings


class AudioDeviceTest(unittest.TestCase):
    def test_clear_input_queue_discards_old_recording(self):
        audio = AudioDevice(Settings(api_key="test"))
        audio.input_queue.put_nowait(b"old audio")

        audio.clear_input_queue()

        self.assertTrue(audio.input_queue.empty())

    def test_busy_tone_has_expected_pcm_length(self):
        settings = Settings(api_key="test", sample_rate=24_000)
        audio = AudioDevice(settings)

        pcm = audio.build_busy_tone(repeats=3)

        # 1回あたり0.5秒の音 + 0.5秒の無音、PCM16なので1標本2バイト。
        self.assertEqual(len(pcm), 24_000 * 3 * 2)
        self.assertNotEqual(pcm[:2_000], bytes(2_000))
        self.assertEqual(pcm[-2_000:], bytes(2_000))

    def test_resamples_device_input_from_48khz_to_24khz(self):
        source = array("h", [0, 100, 200, 300, 400, 500]).tobytes()

        result = resample_pcm16_mono(source, 48_000, 24_000)

        self.assertEqual(array("h", result).tolist(), [0, 200, 400])

    def test_resamples_live_output_from_24khz_to_48khz(self):
        source = array("h", [0, 200, 400]).tobytes()

        result = resample_pcm16_mono(source, 24_000, 48_000)

        self.assertEqual(array("h", result).tolist(), [0, 100, 200, 300, 400, 400])


if __name__ == "__main__":
    unittest.main()
