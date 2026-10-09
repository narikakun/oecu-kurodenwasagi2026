import asyncio
import unittest

from array import array
import json
from pathlib import Path
import tempfile

from kuro_sagi_denwa.audio import (
    AudioDevice,
    AudioDeviceSelector,
    BellRinger,
    resample_pcm16_mono,
)
from kuro_sagi_denwa.config import Settings


class AudioDeviceTest(unittest.TestCase):
    def test_bell_ringer_uses_rotary_phone_ringtone(self):
        ringer = BellRinger(Settings(api_key="test"))

        self.assertEqual(ringer.path.name, "Rotary_Phone-Ringtone01-1.mp3")
        self.assertTrue(ringer.path.is_file())

    def test_device_selector_changes_each_runtime_device(self):
        settings = Settings(api_key="test")
        audio = AudioDevice(settings)
        ringer = BellRinger(settings)
        selector = AudioDeviceSelector(audio, ringer)
        selector._save = lambda: None
        selector._devices = lambda: [
            {"name": "input", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44_100},
            {"name": "handset", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 48_000},
            {"name": "bell", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 96_000},
        ]
        selector.enter()

        selector.handle_digit(0)
        selector.handle_digit(0)
        selector.handle_digit(1)
        selector.handle_digit(1)
        selector.handle_digit(2)
        selector.handle_digit(2)

        self.assertEqual(audio.input_device, 0)
        self.assertEqual(audio.output_device, 1)
        self.assertEqual(ringer.output_device, 2)
        self.assertEqual(audio.input_sample_rate, 44_100)
        self.assertEqual(audio.output_sample_rate, 48_000)
        self.assertEqual(ringer.sample_rate, 96_000)

    def test_device_selector_menu_shows_current_settings_and_returns_after_change(self):
        class FakeDisplay:
            def __init__(self):
                self.events = []

            def publish(self, event):
                self.events.append(event)

        settings = Settings(api_key="test")
        audio = AudioDevice(settings)
        ringer = BellRinger(settings)
        display = FakeDisplay()
        selector = AudioDeviceSelector(audio, ringer, display)
        selector._save = lambda: None
        selector._devices = lambda: [
            {"name": "microphone", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44_100},
            {"name": "handset", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 48_000},
        ]
        selector._describe = lambda device, target: {
            (0, "input"): "0 (microphone)",
            (1, "output"): "1 (handset)",
        }[(device, target)]
        audio.input_device = 0
        audio.output_device = 1
        ringer.output_device = 1

        selector.enter()

        first_menu = display.events[-1]["text"]
        self.assertIn("現在の受話器入力：0 (microphone)", first_menu)
        self.assertIn("現在の受話器出力：1 (handset)", first_menu)
        self.assertIn("48000 Hz", first_menu)
        self.assertIn("0：受話器入力", first_menu)

        selector.handle_digit(1)
        selector.handle_digit(1)

        returned_menu = display.events[-1]["text"]
        self.assertIn("変更する項目をダイヤルしてください", returned_menu)
        self.assertIn("現在の受話器出力：1 (handset)", returned_menu)

    def test_device_selector_saves_names_and_restores_available_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "audio-settings.json"
            settings = Settings(api_key="test")
            audio = AudioDevice(settings)
            ringer = BellRinger(settings)
            selector = AudioDeviceSelector(
                audio, ringer, settings_path=settings_path
            )
            devices = [
                {"name": "microphone", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44_100},
                {"name": "handset", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 48_000},
                {"name": "bell", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 96_000},
            ]
            selector._devices = lambda: devices
            selector._selected_name = lambda target: {
                "input": "microphone",
                "output": "handset",
                "bell": "bell",
            }[target]

            selector._save()

            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8")),
                {"input": "microphone", "output": "handset", "bell": "bell"},
            )

            selector._save = lambda: None
            selector.restore()

            self.assertEqual(audio.input_device, 0)
            self.assertEqual(audio.input_sample_rate, 44_100)
            self.assertEqual(audio.output_device, 1)
            self.assertEqual(audio.output_sample_rate, 48_000)
            self.assertEqual(ringer.output_device, 2)
            self.assertEqual(ringer.sample_rate, 96_000)

    def test_restore_falls_back_only_for_missing_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "audio-settings.json"
            settings_path.write_text(
                json.dumps(
                    {"input": "microphone", "output": "missing", "bell": "missing"}
                ),
                encoding="utf-8",
            )
            settings = Settings(api_key="test")
            audio = AudioDevice(settings)
            ringer = BellRinger(settings)
            selector = AudioDeviceSelector(
                audio, ringer, settings_path=settings_path
            )
            selector._devices = lambda: [
                {"name": "microphone", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44_100}
            ]
            defaulted = []

            def apply_default(target):
                defaulted.append(target)
                selector._apply_device(target, None, 48_000)

            selector._apply_default = apply_default
            selector._save = lambda: None

            selector.restore()

            self.assertEqual(audio.input_device, 0)
            self.assertEqual(audio.input_sample_rate, 44_100)
            self.assertIsNone(audio.output_device)
            self.assertIsNone(ringer.output_device)
            self.assertEqual(defaulted, ["output", "bell"])

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


class BellRingerStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_is_stopped_on_the_same_thread_after_write(self):
        import sys
        import threading
        import time
        from types import SimpleNamespace
        from unittest.mock import patch

        calls = []

        class FakeStream:
            def __init__(self, **kwargs):
                self.writing = False

            def _record(self, name):
                calls.append((name, threading.get_ident(), self.writing))

            def start(self):
                self._record("start")

            def write(self, data):
                self.writing = True
                time.sleep(0.01)
                self.writing = False
                self._record("write")

            def abort(self):
                self._record("abort")

            def close(self):
                self._record("close")

        fake_sd = SimpleNamespace(RawOutputStream=FakeStream)
        fake_miniaudio = SimpleNamespace(
            SampleFormat=SimpleNamespace(SIGNED16=None),
            decode_file=lambda *args, **kwargs: SimpleNamespace(samples=[0] * 48_000),
        )
        with patch.dict(sys.modules, {"sounddevice": fake_sd, "miniaudio": fake_miniaudio}):
            ringer = BellRinger(Settings(api_key="test"))
            await ringer.start()
            await asyncio.sleep(0.05)
            await ringer.stop()

        names = [name for name, _, _ in calls]
        self.assertEqual(names[0], "start")
        self.assertEqual(names[-2:], ["abort", "close"])
        # 書き込み中に停止せず、すべて同じスレッドで操作している。
        self.assertEqual(len({thread for _, thread, _ in calls}), 1)
        self.assertFalse(any(writing for _, _, writing in calls))


if __name__ == "__main__":
    unittest.main()
