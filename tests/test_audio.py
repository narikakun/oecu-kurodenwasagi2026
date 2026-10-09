import asyncio
import unittest

from array import array
import json
from pathlib import Path
import tempfile

from kuro_sagi_denwa.audio import (
    AudienceOutput,
    AudioDevice,
    AudioDeviceSelector,
    BellRinger,
    _resolve_device,
    _supported_sample_rate,
    resample_pcm16_mono,
)
from kuro_sagi_denwa.config import Settings


class AudioDeviceTest(unittest.TestCase):
    def test_resolves_current_index_from_saved_device_name_after_hotplug(self):
        class FakeSoundDevice:
            @staticmethod
            def query_devices(device=None, kind=None):
                devices = [
                    {"name": "new device", "max_output_channels": 1},
                    {"name": "saved speaker", "max_output_channels": 1},
                ]
                return devices if device is None else devices[device]

        self.assertEqual(
            _resolve_device(FakeSoundDevice, 0, "saved speaker", "output"),
            1,
        )

    def test_uses_device_default_rate_when_configured_rate_is_unsupported(self):
        class FakeSoundDevice:
            @staticmethod
            def check_output_settings(**kwargs):
                if kwargs["samplerate"] != 44_100:
                    raise ValueError("Sample format not supported")

            @staticmethod
            def query_devices(device=None, kind=None):
                return {"default_samplerate": 44_100}

        self.assertEqual(
            _supported_sample_rate(FakeSoundDevice, 2, "output", 48_000),
            44_100,
        )

    def test_bell_ringer_uses_rotary_phone_ringtone(self):
        ringer = BellRinger(Settings(api_key="test"))

        self.assertEqual(ringer.path.name, "Rotary_Phone-Ringtone01-1.mp3")
        self.assertTrue(ringer.path.is_file())

    def test_device_selector_changes_each_runtime_device(self):
        settings = Settings(api_key="test")
        audio = AudioDevice(settings)
        ringer = BellRinger(settings)
        audience = AudienceOutput(settings)
        selector = AudioDeviceSelector(audio, ringer, audience=audience)
        selector._save = lambda: None
        selector._devices = lambda: [
            {"name": "input", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44_100},
            {"name": "handset", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 48_000},
            {"name": "bell", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 96_000},
            {"name": "audience", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 48_000},
        ]
        selector.enter()

        selector.handle_digit(0)
        selector.handle_digit(0)
        selector.handle_digit(1)
        selector.handle_digit(1)
        selector.handle_digit(2)
        selector.handle_digit(2)
        selector.handle_digit(3)
        selector.handle_digit(3)

        self.assertEqual(audio.input_device, 0)
        self.assertEqual(audio.output_device, 1)
        self.assertEqual(ringer.output_device, 2)
        self.assertEqual(audience.output_device, 3)
        self.assertEqual(audio.input_sample_rate, 44_100)
        self.assertEqual(audio.output_sample_rate, 48_000)
        self.assertEqual(ringer.sample_rate, 96_000)
        self.assertEqual(audience.sample_rate, 48_000)

    def test_device_selector_menu_shows_current_settings_and_returns_after_change(self):
        class FakeDisplay:
            def __init__(self):
                self.events = []

            def publish(self, event):
                self.events.append(event)

        settings = Settings(api_key="test")
        audio = AudioDevice(settings)
        ringer = BellRinger(settings)
        audience = AudienceOutput(settings)
        display = FakeDisplay()
        selector = AudioDeviceSelector(audio, ringer, display, audience)
        selector._save = lambda: None
        selector._devices = lambda: [
            {"name": "microphone", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44_100},
            {"name": "handset", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 48_000},
        ]
        selector._describe = lambda device, target: {
            (0, "input"): "0 (microphone)",
            (1, "output"): "1 (handset)",
            (2, "output"): "2 (audience)",
        }[(device, target)]
        audio.input_device = 0
        audio.output_device = 1
        ringer.output_device = 1
        audience.output_device = 2

        selector.enter()

        first_menu = display.events[-1]["text"]
        self.assertIn("現在の受話器入力：0 (microphone)", first_menu)
        self.assertIn("現在の受話器出力：1 (handset)", first_menu)
        self.assertIn("48000 Hz", first_menu)
        self.assertIn("0：受話器入力", first_menu)
        self.assertIn("3：観客用出力", first_menu)
        self.assertIn("6：観客用音量", first_menu)

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
            audience = AudienceOutput(settings)
            selector = AudioDeviceSelector(
                audio, ringer, audience=audience, settings_path=settings_path
            )
            devices = [
                {"name": "microphone", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44_100},
                {"name": "handset", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 48_000},
                {"name": "bell", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 96_000},
                {"name": "audience", "max_input_channels": 0, "max_output_channels": 1, "default_samplerate": 48_000},
            ]
            selector._devices = lambda: devices
            selector._selected_name = lambda target: {
                "input": "microphone",
                "output": "handset",
                "bell": "bell",
                "audience": "audience",
            }[target]

            selector._save()

            self.assertEqual(
                json.loads(settings_path.read_text(encoding="utf-8")),
                {
                    "input": "microphone",
                    "output": "handset",
                    "bell": "bell",
                    "audience": "audience",
                    "output_volume": 1.0,
                    "bell_volume": 0.7,
                    "audience_volume": 0.7,
                },
            )

            selector._save = lambda: None
            selector.restore()

            self.assertEqual(audio.input_device, 0)
            self.assertEqual(audio.input_sample_rate, 44_100)
            self.assertEqual(audio.output_device, 1)
            self.assertEqual(audio.output_sample_rate, 48_000)
            self.assertEqual(ringer.output_device, 2)
            self.assertEqual(ringer.sample_rate, 96_000)
            self.assertEqual(audience.output_device, 3)
            self.assertEqual(audience.sample_rate, 48_000)

    def test_device_selector_changes_and_saves_volumes(self):
        settings = Settings(api_key="test")
        audio = AudioDevice(settings)
        ringer = BellRinger(settings)
        audience = AudienceOutput(settings)
        selector = AudioDeviceSelector(audio, ringer, audience=audience)
        selector._save = lambda: None
        selector._show_menu = lambda: None

        selector.enter()
        selector.handle_digit(4)
        selector.handle_digit(3)
        selector.handle_digit(5)
        selector.handle_digit(6)
        selector.handle_digit(6)
        selector.handle_digit(9)

        self.assertAlmostEqual(audio.output_volume, 3 / 9)
        self.assertAlmostEqual(ringer.volume, 6 / 9)
        self.assertEqual(audience.volume, 1.0)

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
            audience = AudienceOutput(settings)
            selector = AudioDeviceSelector(
                audio, ringer, audience=audience, settings_path=settings_path
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
            self.assertIsNone(audience.output_device)
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
        streams = []

        class FakeStream:
            def __init__(self, **kwargs):
                self.writing = False
                self.device = kwargs.get("device")
                self.write_count = 0
                streams.append(self)

            def _record(self, name):
                calls.append((name, threading.get_ident(), self.writing))

            def start(self):
                self._record("start")

            def write(self, data):
                self.writing = True
                self.write_count += 1
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
            settings = Settings(api_key="test", audience_output_device=3)
            audience = AudienceOutput(settings)
            ringer = BellRinger(settings, audience)
            await ringer.start()
            await asyncio.sleep(0.05)
            await ringer.stop()

        names = [name for name, _, _ in calls]
        self.assertEqual(names[:2], ["start", "start"])
        self.assertEqual(names[-4:], ["abort", "close", "abort", "close"])
        self.assertEqual(len(streams), 2)
        self.assertTrue(all(stream.write_count > 0 for stream in streams))
        # 書き込み中に停止せず、すべて同じスレッドで操作している。
        self.assertEqual(len({thread for _, thread, _ in calls}), 1)
        self.assertFalse(any(writing for _, _, writing in calls))


class AudioDeviceAudienceTest(unittest.IsolatedAsyncioTestCase):
    async def test_handset_audio_is_scaled_and_copied_to_audience(self):
        class FakeStream:
            def __init__(self):
                self.writes = []

            def write(self, data):
                self.writes.append(data)

        settings = Settings(
            api_key="test",
            sample_rate=24_000,
            audio_device_sample_rate=24_000,
            audio_output_volume=0.5,
            audience_output_device=3,
            audience_device_sample_rate=24_000,
            audience_volume=0.25,
        )
        audience = AudienceOutput(settings)
        audio = AudioDevice(settings, audience)
        handset_stream = FakeStream()
        audience_stream = FakeStream()
        audio._output_stream = handset_stream
        audio._audience_stream = audience_stream
        task = asyncio.create_task(audio._play_loop())

        await audio.play(array("h", [1000, -1000]).tobytes())
        await audio.output_queue.join()
        await audio.output_queue.put(None)
        await task

        self.assertEqual(array("h", handset_stream.writes[0]).tolist(), [500, -500])
        self.assertEqual(array("h", audience_stream.writes[0]).tolist(), [250, -250])

    async def test_audience_continues_when_handset_output_is_unplugged(self):
        class DisconnectedStream:
            def write(self, data):
                raise RuntimeError("device unavailable")

            def abort(self):
                pass

            def close(self):
                pass

        class WorkingStream:
            def __init__(self):
                self.writes = []

            def write(self, data):
                self.writes.append(data)

        settings = Settings(
            api_key="test",
            sample_rate=24_000,
            audio_device_sample_rate=24_000,
            audience_output_device=3,
            audience_device_sample_rate=24_000,
        )
        audience = AudienceOutput(settings)
        audio = AudioDevice(settings, audience)
        audience_stream = WorkingStream()
        audio._output_stream = DisconnectedStream()
        audio._audience_stream = audience_stream
        task = asyncio.create_task(audio._play_loop())

        await audio.play(array("h", [1000]).tobytes())
        await audio.output_queue.join()
        await audio.play(array("h", [2000]).tobytes())
        await audio.output_queue.join()
        await audio.output_queue.put(None)
        await task

        self.assertIsNone(audio._output_stream)
        self.assertEqual(len(audience_stream.writes), 2)


if __name__ == "__main__":
    unittest.main()
