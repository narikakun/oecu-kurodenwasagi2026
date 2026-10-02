"""USB受話器の録音と再生を扱う。"""

from __future__ import annotations

import asyncio
import logging
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
                return
            if self._output_stream is not None:
                await asyncio.to_thread(self._output_stream.write, data)

    async def play(self, data: bytes) -> None:
        if self.output_queue.full():
            # 再生が追いつかない場合も、無制限にメモリを使わないよう制限する。
            logger.warning("音声再生キューが満杯のため、一部の音声を破棄します")
            return
        await self.output_queue.put(data)

    async def stop(self) -> None:
        if self._play_task is not None:
            # 受話器を置いたら、まだ再生していない返答は流さずに破棄する。
            while not self.output_queue.empty():
                self.output_queue.get_nowait()
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
