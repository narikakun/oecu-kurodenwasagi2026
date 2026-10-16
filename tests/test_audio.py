import asyncio
import sys
import unittest

from array import array
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from kuro_sagi_denwa.audio import (
    AudienceOutput,
    AudioDevice,
    AudioDeviceSelector,
    BellRinger,
    _resolve_device,
    _supported_sample_rate,
    resample_pcm16_mono,
    scale_pcm16,
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

    def test_bell_output_availability_uses_current_device(self):
        fake_sd = SimpleNamespace(
            query_devices=lambda device=None, kind=None: {
                "name": "bell",
                "max_output_channels": 2,
            }
        )
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            ringer = BellRinger(Settings(api_key="test", bell_output_device=2))
            self.assertTrue(ringer.output_available())

    def test_missing_named_bell_output_is_unavailable(self):
        class FakeSoundDevice:
            @staticmethod
            def query_devices(device=None, kind=None):
                if device is None and kind is None:
                    return [{"name": "other", "max_output_channels": 1}]
                raise ValueError("device not found")

        with patch.dict(sys.modules, {"sounddevice": FakeSoundDevice}):
            ringer = BellRinger(
                Settings(api_key="test", bell_output_device="missing bell")
            )
            self.assertFalse(ringer.output_available())

    def test_bell_output_availability_uses_default_output(self):
        query_devices = Mock(return_value={"max_output_channels": 2})
        fake_sd = SimpleNamespace(query_devices=query_devices)
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            ringer = BellRinger(Settings(api_key="test"))
            self.assertTrue(ringer.output_available())
        query_devices.assert_called_once_with(None, kind="output")

    def test_missing_default_or_selected_bell_output_is_unavailable(self):
        for device in (None, 2):
            with self.subTest(device=device):
                fake_sd = SimpleNamespace(
                    query_devices=Mock(side_effect=ValueError("device not found"))
                )
                with patch.dict(sys.modules, {"sounddevice": fake_sd}):
                    ringer = BellRinger(
                        Settings(api_key="test", bell_output_device=device)
                    )
                    self.assertFalse(ringer.output_available())

    def test_bell_device_without_output_channels_is_unavailable(self):
        fake_sd = SimpleNamespace(
            query_devices=Mock(return_value={"max_output_channels": 0})
        )
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            ringer = BellRinger(Settings(api_key="test", bell_output_device=2))
            self.assertFalse(ringer.output_available())

    def test_unsupported_bell_sample_format_is_unavailable(self):
        fake_sd = SimpleNamespace(
            query_devices=Mock(return_value={
                "max_output_channels": 2, "default_samplerate": 44_100,
            }),
            check_output_settings=Mock(
                side_effect=ValueError("Sample format not supported")
            ),
        )
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            ringer = BellRinger(Settings(api_key="test"))
            self.assertFalse(ringer.output_available())
        self.assertEqual(fake_sd.check_output_settings.call_count, 2)

    def test_bell_output_supporting_default_rate_is_available(self):
        fake_sd = SimpleNamespace(
            query_devices=Mock(return_value={
                "max_output_channels": 2, "default_samplerate": 44_100,
            }),
            check_output_settings=Mock(side_effect=[ValueError("unsupported"), None]),
        )
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            ringer = BellRinger(Settings(api_key="test"))
            self.assertTrue(ringer.output_available())

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

        first_menu = display.events[-1]
        self.assertEqual(first_menu["title"], "音声設定")
        menu_text = json.dumps(first_menu, ensure_ascii=False)
        self.assertIn("受話器入力　0 (microphone)", menu_text)
        self.assertIn("受話器出力　1 (handset)", menu_text)
        self.assertIn("48000 Hz", menu_text)
        self.assertIn("0 受話器入力", first_menu["choices"])
        self.assertIn("3 観客用出力", first_menu["choices"])
        self.assertIn("6 観客用音量", first_menu["choices"])
        self.assertEqual(first_menu["footer"], "7：設定を閉じる")

        selector.handle_digit(1)
        picker = display.events[-1]
        self.assertEqual(picker["title"], "受話器出力の設定")
        self.assertEqual(picker["subtitle"], "いま設定している項目：受話器出力")
        self.assertIn("7：ひとつ前へ戻る", picker["footer"])
        selector.handle_digit(1)

        returned_menu = display.events[-1]
        self.assertEqual(returned_menu["subtitle"], "設定する項目を選んでください")
        self.assertIn("受話器出力　1 (handset)", json.dumps(returned_menu, ensure_ascii=False))

    def test_device_selector_digit_seven_goes_back_then_closes_settings(self):
        class FakeDisplay:
            def __init__(self):
                self.events = []

            def publish(self, event):
                self.events.append(event)

        settings = Settings(api_key="test")
        selector = AudioDeviceSelector(
            AudioDevice(settings), BellRinger(settings), FakeDisplay()
        )
        selector._devices = lambda: []

        selector.enter()
        selector.handle_digit(4)
        self.assertEqual(selector._pending_target, "output")

        selector.handle_digit(7)
        self.assertTrue(selector.active)
        self.assertIsNone(selector._pending_target)
        self.assertEqual(selector.display.events[-1]["title"], "音声設定")

        selector.handle_digit(7)
        self.assertFalse(selector.active)
        self.assertEqual(
            selector.display.events[-1], {"type": "settings", "active": False}
        )

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

    def test_capture_backlog_keeps_recent_audio_within_200ms(self):
        audio = AudioDevice(Settings(api_key="test", audio_device_sample_rate=24_000))
        audio._loop = SimpleNamespace(call_soon_threadsafe=lambda callback: callback())
        for index in range(100):
            audio._capture_callback(array("h", [index] * 480).tobytes(), 480, None, None)
        self.assertEqual(audio.input_queue.qsize(), 10)
        self.assertEqual(audio.input_drop_streak, 90)
        packet = audio.input_queue.get_nowait()
        self.assertEqual(array("h", packet)[0], 90)
        audio.input_queue.task_done()
        audio.clear_input_queue()
        self.assertEqual(audio.input_queue._unfinished_tasks, 0)

        audio._capture_callback(array("h", [100] * 480).tobytes(), 480, None, None)
        self.assertEqual(audio.input_drop_streak, 0)

    def test_vectorized_volume_preserves_truncation_and_clips(self):
        source = array("h", [-32768, -3, 0, 3, 32767])
        self.assertEqual(array("h", scale_pcm16(source, 0.5)).tolist(), [-16384, -1, 0, 1, 16383])
        self.assertEqual(array("h", scale_pcm16(source.tobytes(), 2)).tolist(), [-32768, -6, 0, 6, 32767])

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
    async def test_stream_open_failure_reaches_playback_watcher(self):
        fake_sd = SimpleNamespace(
            RawOutputStream=Mock(side_effect=ValueError("Sample format not supported"))
        )
        with patch.dict(sys.modules, {
            "sounddevice": fake_sd, "miniaudio": SimpleNamespace(),
        }):
            ringer = BellRinger(Settings(api_key="test"))
            await ringer.start()
            with self.assertRaisesRegex(RuntimeError, "ベル用出力を使用できません"):
                await asyncio.wait_for(ringer.wait_until_stopped(), timeout=1)
            await ringer.stop()

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
    async def test_full_output_worker_applies_backpressure_without_dropping(self):
        import threading

        class BlockingWorker:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
                self.received = []

            def put(self, data):
                self.entered.set()
                self.release.wait(timeout=1)
                self.received.append(data)

        audio = AudioDevice(Settings(api_key="test"))
        worker = BlockingWorker()
        audio._output_worker = worker
        task = asyncio.create_task(audio._play_loop())
        packet = bytes(960)

        try:
            await audio.play(packet)
            self.assertTrue(await asyncio.to_thread(worker.entered.wait, 0.5))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(audio.output_queue.join(), timeout=0.01)
            worker.release.set()
            await asyncio.wait_for(audio.output_queue.join(), timeout=0.5)
            self.assertEqual(worker.received, [packet])
        finally:
            worker.release.set()
            audio._output_worker = None
            await audio.output_queue.put(None)
            await task

    async def test_logs_output_queue_and_playback_start_timing(self):
        class FakeStream:
            def start(self):
                pass

            def write(self, data):
                return False

            def stop(self):
                pass

            def close(self):
                pass

        audio = AudioDevice(Settings(api_key="test", audio_device_sample_rate=24_000))
        audio._output_stream = FakeStream()
        task = asyncio.create_task(audio._play_loop())

        with self.assertLogs("kuro_sagi_denwa.audio", level="INFO") as logs:
            await audio.play(array("h", [1000]).tobytes())
            await audio.output_queue.join()
            await audio.wait_until_played()
            await audio.output_queue.put(None)
            await task

        output = "\n".join(logs.output)
        self.assertIn("同期計測 output_queued output=受話器出力 burst=1", output)
        self.assertIn("同期計測 output_started output=受話器出力 burst=1", output)
        self.assertIn("queue_wait_ms=", output)

    async def test_output_conversion_does_not_block_event_loop(self):
        import threading

        entered = threading.Event()
        release = threading.Event()
        completed = []
        audio = AudioDevice(Settings(api_key="test", audio_device_sample_rate=24_000))
        audio._output_stream = Mock(write=Mock(return_value=False))

        def slow_scale(data, volume):
            entered.set()
            completed.append(release.wait(timeout=1))
            return data

        with patch("kuro_sagi_denwa.audio.scale_pcm16", side_effect=slow_scale):
            task = asyncio.create_task(audio._play_loop())
            try:
                await audio.play(bytes(960))
                self.assertTrue(await asyncio.to_thread(entered.wait, 0.5))
                # 変換が待機中でもイベントループから解放できることを確認する。
                release.set()
                await audio.output_queue.put(None)
                await asyncio.wait_for(task, timeout=2)
                self.assertEqual(completed, [True])
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await audio._stop_output_workers(discard=True)

    async def test_plays_audio_file_once_and_waits_until_finished(self):
        fake_miniaudio = SimpleNamespace(
            SampleFormat=SimpleNamespace(SIGNED16=None),
            decode_file=lambda *args, **kwargs: SimpleNamespace(
                samples=[1000, -1000]
            ),
        )
        audio = AudioDevice(Settings(api_key="test"))
        audio.play = AsyncMock()
        audio.wait_until_played = AsyncMock()

        with patch.dict(sys.modules, {"miniaudio": fake_miniaudio}):
            await audio.play_audio_file("disconnect.mp3", volume=0.5)

        audio.play.assert_awaited_once_with(array("h", [500, -500]).tobytes())
        audio.wait_until_played.assert_awaited_once_with()

    async def test_plays_long_audio_file_in_stream_sized_blocks(self):
        samples = list(range(1_000))
        fake_miniaudio = SimpleNamespace(
            SampleFormat=SimpleNamespace(SIGNED16=None),
            decode_file=lambda *args, **kwargs: SimpleNamespace(samples=samples),
        )
        audio = AudioDevice(
            Settings(api_key="test", sample_rate=24_000, block_ms=20)
        )
        audio.play = AsyncMock()
        audio.wait_until_played = AsyncMock()

        with patch.dict(sys.modules, {"miniaudio": fake_miniaudio}):
            with self.assertLogs("kuro_sagi_denwa.audio", level="INFO") as logs:
                await audio.play_audio_file("disconnect.wav")

        chunks = [call.args[0] for call in audio.play.await_args_list]
        self.assertEqual([len(chunk) for chunk in chunks], [960, 960, 80])
        self.assertEqual(b"".join(chunks), array("h", samples).tobytes())
        audio.wait_until_played.assert_awaited_once_with()
        self.assertIn("blocks=3", "\n".join(logs.output))
        self.assertIn("音声ファイルの再生が完了", "\n".join(logs.output))

    async def test_slow_audience_output_does_not_block_handset_output(self):
        import threading

        audience_writing = threading.Event()
        release_audience = threading.Event()

        class HandsetStream:
            def __init__(self):
                self.writes = []

            def start(self):
                pass

            def write(self, data):
                self.writes.append(data)
                return False

            def stop(self):
                pass

            def close(self):
                pass

        class SlowAudienceStream(HandsetStream):
            def write(self, data):
                audience_writing.set()
                release_audience.wait(timeout=1)
                return super().write(data)

        settings = Settings(
            api_key="test",
            sample_rate=24_000,
            audio_device_sample_rate=24_000,
            audience_output_device=3,
            audience_device_sample_rate=24_000,
        )
        audio = AudioDevice(settings, AudienceOutput(settings))
        handset_stream = HandsetStream()
        audience_stream = SlowAudienceStream()
        audio._output_stream = handset_stream
        audio._audience_stream = audience_stream
        task = asyncio.create_task(audio._play_loop())

        try:
            for value in range(3):
                await audio.play(array("h", [value]).tobytes())
            await audio.output_queue.join()
            self.assertTrue(await asyncio.to_thread(audience_writing.wait, 0.2))
            for _ in range(20):
                if len(handset_stream.writes) == 3:
                    break
                await asyncio.sleep(0)

            self.assertEqual(len(handset_stream.writes), 3)
            self.assertEqual(len(audience_stream.writes), 0)
        finally:
            release_audience.set()
            await audio.output_queue.put(None)
            await task

    async def test_each_output_stream_stays_on_its_own_fixed_thread(self):
        import threading

        class ThreadRecordingStream:
            def __init__(self):
                self.calls = []

            def _record(self, action):
                self.calls.append((action, threading.get_ident()))

            def start(self):
                self._record("start")

            def write(self, data):
                self._record("write")
                return False

            def stop(self):
                self._record("stop")

            def close(self):
                self._record("close")

        settings = Settings(
            api_key="test",
            sample_rate=24_000,
            audio_device_sample_rate=24_000,
            audience_output_device=3,
            audience_device_sample_rate=24_000,
        )
        audio = AudioDevice(settings, AudienceOutput(settings))
        handset_stream = ThreadRecordingStream()
        audience_stream = ThreadRecordingStream()
        audio._output_stream = handset_stream
        audio._audience_stream = audience_stream
        task = asyncio.create_task(audio._play_loop())

        for value in range(4):
            await audio.play(array("h", [value]).tobytes())
        await audio.output_queue.join()
        await audio.output_queue.put(None)
        await task

        for stream in (handset_stream, audience_stream):
            self.assertEqual(
                [action for action, _ in stream.calls],
                ["start", "write", "write", "write", "write", "stop", "close"],
            )
            self.assertEqual(len({thread_id for _, thread_id in stream.calls}), 1)

    async def test_audience_underflow_is_logged(self):
        class UnderflowStream:
            def start(self):
                pass

            def write(self, data):
                return True

            def stop(self):
                pass

            def close(self):
                pass

        settings = Settings(
            api_key="test",
            sample_rate=24_000,
            audience_output_device=3,
            audience_device_sample_rate=24_000,
        )
        audio = AudioDevice(settings, AudienceOutput(settings))
        audio._audience_stream = UnderflowStream()
        task = asyncio.create_task(audio._play_loop())

        with self.assertLogs("kuro_sagi_denwa.audio", level="WARNING") as logs:
            await audio.play(array("h", [1000]).tobytes())
            await audio.output_queue.join()
            await audio.output_queue.put(None)
            await task

        self.assertTrue(any("観客用出力で音声アンダーフロー" in line for line in logs.output))

    async def test_handset_audio_is_scaled_and_copied_to_audience(self):
        class FakeStream:
            def __init__(self):
                self.writes = []

            def start(self):
                pass

            def write(self, data):
                self.writes.append(data)
                return False

            def stop(self):
                pass

            def close(self):
                pass

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
            def start(self):
                pass

            def write(self, data):
                raise RuntimeError("device unavailable")

            def abort(self):
                pass

            def close(self):
                pass

        class WorkingStream:
            def __init__(self):
                self.writes = []

            def start(self):
                pass

            def write(self, data):
                self.writes.append(data)
                return False

            def stop(self):
                pass

            def close(self):
                pass

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
