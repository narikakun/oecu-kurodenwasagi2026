"""USB受話器の録音と再生を扱う。"""

from __future__ import annotations

import asyncio
import logging
import math
from array import array
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)


class AudioDevice:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
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
        data = bytes(indata)

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
        frames = self.settings.sample_rate * self.settings.block_ms // 1000

        self._input_stream = sd.RawInputStream(
            samplerate=self.settings.sample_rate,
            blocksize=frames,
            channels=1,
            dtype="int16",
            device=self.settings.audio_input_device,
            callback=self._capture_callback,
        )
        self._output_stream = sd.RawOutputStream(
            samplerate=self.settings.sample_rate,
            blocksize=frames,
            channels=1,
            dtype="int16",
            device=self.settings.audio_output_device,
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
                await asyncio.to_thread(self._output_stream.write, data)
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
