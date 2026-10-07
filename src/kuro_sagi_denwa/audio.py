"""USB受話器の録音と再生を扱う。"""

from __future__ import annotations

import asyncio
import logging
import math
from array import array
from pathlib import Path
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)


class BellRinger:
    """受話器とは別の出力デバイスで黒電話のベル音を繰り返す。"""

    def __init__(self, settings: Settings, path: str | Path | None = None) -> None:
        self.settings = settings
        self.path = Path(path) if path else (
            Path(__file__).parent / "assets" / "audio" / "Rotary_Phone-Ringtone01-1.mp3"
        )
        self.output_device = settings.bell_output_device
        self._task: asyncio.Task[None] | None = None

    @property
    def is_ringing(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        await self.stop()
        self._task = asyncio.create_task(self._ring_loop())
        self._task.add_done_callback(self._report_failure)

    @staticmethod
    def _report_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "ベル音の再生に失敗しました",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _ring_loop(self) -> None:
        import miniaudio
        import sounddevice as sd

        sound = await asyncio.to_thread(
            miniaudio.decode_file,
            str(self.path),
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=self.settings.bell_device_sample_rate,
        )
        samples = array("h", sound.samples)
        for index, sample in enumerate(samples):
            samples[index] = max(
                -32768, min(32767, int(sample * self.settings.bell_volume))
            )
        pcm = samples.tobytes()
        bytes_per_block = (
            self.settings.bell_device_sample_rate * self.settings.block_ms // 1000 * 2
        )
        stream = sd.RawOutputStream(
            samplerate=self.settings.bell_device_sample_rate,
            blocksize=self.settings.bell_device_sample_rate
            * self.settings.block_ms
            // 1000,
            channels=1,
            dtype="int16",
            device=self.output_device,
        )
        try:
            stream.start()
            logger.info("ベルを鳴らします")
            while True:
                for offset in range(0, len(pcm), bytes_per_block):
                    await asyncio.to_thread(
                        stream.write, pcm[offset : offset + bytes_per_block]
                    )
        except asyncio.CancelledError:
            raise
        finally:
            await asyncio.to_thread(stream.stop)
            await asyncio.to_thread(stream.close)
            logger.info("ベルを停止しました")


class AudioDeviceSelector:
    """ロータリーダイヤルで実行中の音声デバイスを選択する。"""

    TARGETS = {
        0: ("受話器入力", "input"),
        1: ("受話器出力", "output"),
        2: ("ベル出力", "bell"),
    }

    def __init__(self, audio: AudioDevice, ringer: BellRinger) -> None:
        self.audio = audio
        self.ringer = ringer
        self.active = False
        self._pending_target: str | None = None

    def show_current(self) -> None:
        logger.info(
            "音声デバイス設定: 受話器入力=%s / 受話器出力=%s / ベル出力=%s",
            self._describe(self.audio.input_device, "input"),
            self._describe(self.audio.output_device, "output"),
            self._describe(self.ringer.output_device, "output"),
        )

    def enter(self) -> None:
        self.active = True
        self._pending_target = None
        logger.info(
            "音声デバイス設定モード: 0=受話器入力, 1=受話器出力, 2=ベル出力"
        )

    def exit(self) -> None:
        if self.active:
            logger.info("音声デバイス設定モードを終了します")
        self.active = False
        self._pending_target = None

    def handle_digit(self, digit: int) -> None:
        if self._pending_target is None:
            target = self.TARGETS.get(digit)
            if target is None:
                logger.warning("0、1、2のいずれかを回してください")
                return
            label, self._pending_target = target
            logger.info("%sを選択します。次にデバイス番号を回してください", label)
            self._show_available(self._pending_target)
            return

        if not self._supports(digit, self._pending_target):
            logger.warning("デバイス%dは選択した用途では使用できません", digit)
            self._show_available(self._pending_target)
            return

        label = next(
            name for name, target in self.TARGETS.values() if target == self._pending_target
        )
        if self._pending_target == "input":
            self.audio.input_device = digit
        elif self._pending_target == "output":
            self.audio.output_device = digit
        else:
            self.ringer.output_device = digit
        logger.info("%sをデバイス%dへ変更しました", label, digit)
        self._pending_target = None
        self.show_current()
        logger.info("続けて0、1、2を回すか、受話器を上げて設定を終了してください")

    @staticmethod
    def _devices() -> list[dict[str, Any]]:
        import sounddevice as sd

        return list(sd.query_devices())

    def _show_available(self, target: str) -> None:
        channel_key = "max_input_channels" if target == "input" else "max_output_channels"
        available = [
            f"{index}={device['name']}"
            for index, device in enumerate(self._devices())
            if index <= 9 and device[channel_key] > 0
        ]
        logger.info("選択可能なデバイス: %s", ", ".join(available) or "なし")

    def _supports(self, index: int, target: str) -> bool:
        devices = self._devices()
        if not 0 <= index < len(devices):
            return False
        channel_key = "max_input_channels" if target == "input" else "max_output_channels"
        return devices[index][channel_key] > 0

    def _describe(self, device: str | int | None, target: str) -> str:
        try:
            import sounddevice as sd

            if device is None:
                default_input, default_output = sd.default.device
                device = default_input if target == "input" else default_output
            info = sd.query_devices(device)
            return f"{device} ({info['name']})"
        except Exception as error:
            return f"{device!r} (取得失敗: {error})"


def resample_pcm16_mono(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """モノラルPCM16を線形補間で別のサンプルレートへ変換する。"""
    if source_rate == target_rate or not pcm:
        return pcm

    source = array("h")
    source.frombytes(pcm)
    if not source:
        return b""

    target_length = max(1, round(len(source) * target_rate / source_rate))
    target = array("h")
    for target_index in range(target_length):
        position = target_index * source_rate / target_rate
        left_index = min(int(position), len(source) - 1)
        right_index = min(left_index + 1, len(source) - 1)
        fraction = position - left_index
        value = round(
            source[left_index]
            + (source[right_index] - source[left_index]) * fraction
        )
        target.append(value)
    return target.tobytes()


class AudioDevice:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.input_device = settings.audio_input_device
        self.output_device = settings.audio_output_device
        self.input_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self.output_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=200)
        self._input_stream: Any = None
        self._output_stream: Any = None
        self._play_task: asyncio.Task[None] | None = None
        self._hold_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _capture_callback(self, indata, frames, time_info, status) -> None:
        if status:
            logger.warning("音声入力の警告: %s", status)
        if self._loop is None:
            return
        data = resample_pcm16_mono(
            bytes(indata),
            self.settings.audio_device_sample_rate,
            self.settings.sample_rate,
        )

        def enqueue() -> None:
            if self.input_queue.full():
                # 遅延が積み上がるより、古い音声を捨てて現在へ追いつく方を優先する。
                try:
                    self.input_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self.input_queue.put_nowait(data)

        self._loop.call_soon_threadsafe(enqueue)

    async def start(self) -> None:
        import sounddevice as sd

        self._loop = asyncio.get_running_loop()
        frames = self.settings.audio_device_sample_rate * self.settings.block_ms // 1000

        self._input_stream = sd.RawInputStream(
            samplerate=self.settings.audio_device_sample_rate,
            blocksize=frames,
            channels=1,
            dtype="int16",
            device=self.input_device,
            callback=self._capture_callback,
        )
        self._output_stream = sd.RawOutputStream(
            samplerate=self.settings.audio_device_sample_rate,
            blocksize=frames,
            channels=1,
            dtype="int16",
            device=self.output_device,
        )
        self._input_stream.start()
        self._output_stream.start()
        self._play_task = asyncio.create_task(self._play_loop())
        logger.info("USB受話器の録音・再生を開始しました")

    async def _play_loop(self) -> None:
        while True:
            data = await self.output_queue.get()
            if data is None:
                self.output_queue.task_done()
                return
            if self._output_stream is not None:
                device_data = resample_pcm16_mono(
                    data,
                    self.settings.sample_rate,
                    self.settings.audio_device_sample_rate,
                )
                await asyncio.to_thread(self._output_stream.write, device_data)
            self.output_queue.task_done()

    async def play(self, data: bytes) -> None:
        if self.output_queue.full():
            # 再生が追いつかない場合も、無制限にメモリを使わないよう制限する。
            logger.warning("音声再生キューが満杯のため、一部の音声を破棄します")
            return
        await self.output_queue.put(data)

    async def wait_until_played(self) -> None:
        """再生待ちの音声がなくなるまで待つ。"""
        await self.output_queue.join()

    def clear_input_queue(self) -> None:
        """担当切替前に録音された古い音声を、新しい担当へ送らないよう捨てる。"""
        while not self.input_queue.empty():
            self.input_queue.get_nowait()

    async def play_busy_tone(self, repeats: int = 3) -> None:
        """相手側から切られたことを示す「プー、プー」音を再生する。"""
        tone = self.build_busy_tone(repeats)
        await self.play(tone)
        await self.wait_until_played()

    async def start_hold_music(self, path: str, volume: float = 0.15) -> None:
        """MP3の保留音を小さな音量で繰り返し再生する。"""
        await self.stop_hold_music()

        import miniaudio

        sound = await asyncio.to_thread(
            miniaudio.decode_file,
            path,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=self.settings.sample_rate,
        )
        samples = array("h", sound.samples)
        for index, sample in enumerate(samples):
            samples[index] = max(-32768, min(32767, int(sample * volume)))

        self._hold_task = asyncio.create_task(self._hold_music_loop(samples.tobytes()))
        logger.info("担当切替の保留音を開始しました")

    async def _hold_music_loop(self, pcm: bytes) -> None:
        """停止されるまで、短い単位で保留音をキューへ送る。"""
        bytes_per_block = (
            self.settings.sample_rate * self.settings.block_ms // 1000 * 2
        )
        try:
            while True:
                for offset in range(0, len(pcm), bytes_per_block):
                    await self.output_queue.put(pcm[offset : offset + bytes_per_block])
                    # 一度に大量の保留音を積まず、停止時にすぐ切り替えられるようにする。
                    await self.output_queue.join()
        except asyncio.CancelledError:
            return

    async def stop_hold_music(self) -> None:
        """保留音を止め、まだ再生していない保留音を捨てる。"""
        if self._hold_task is not None:
            self._hold_task.cancel()
            await asyncio.gather(self._hold_task, return_exceptions=True)
            self._hold_task = None

        while not self.output_queue.empty():
            self.output_queue.get_nowait()
            self.output_queue.task_done()

    def build_busy_tone(self, repeats: int = 3) -> bytes:
        """400Hzを0.5秒鳴らし、0.5秒休む話中音をPCM16で作る。"""
        samples = array("h")
        tone_samples = self.settings.sample_rate // 2
        silence_samples = self.settings.sample_rate // 2
        amplitude = 8_000

        for _ in range(repeats):
            for index in range(tone_samples):
                value = int(
                    amplitude
                    * math.sin(2 * math.pi * 400 * index / self.settings.sample_rate)
                )
                samples.append(value)
            samples.extend([0] * silence_samples)
        return samples.tobytes()

    async def stop(self) -> None:
        await self.stop_hold_music()
        if self._play_task is not None:
            # 受話器を置いたら、まだ再生していない返答は流さずに破棄する。
            while not self.output_queue.empty():
                self.output_queue.get_nowait()
                self.output_queue.task_done()
            await self.output_queue.put(None)
            await self._play_task
            self._play_task = None

        for stream in (self._input_stream, self._output_stream):
            if stream is not None:
                await asyncio.to_thread(stream.stop)
                await asyncio.to_thread(stream.close)

        self._input_stream = None
        self._output_stream = None
        self._loop = None

        while not self.input_queue.empty():
            self.input_queue.get_nowait()
        logger.info("USB受話器を停止しました")
