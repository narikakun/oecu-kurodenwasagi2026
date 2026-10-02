"""GPT-Liveの文字起こし断片をターミナル表示用にまとめる。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("conversation")


@dataclass
class _SpeakerBuffer:
    label: str
    text: str = ""
    flush_task: asyncio.Task[None] | None = None


class TranscriptLogger:
    """発話完了イベントの代わりに、無音相当の時間で一行へまとめる。"""

    def __init__(
        self,
        flush_seconds: float = 0.8,
        on_flush: Callable[[str, str], None] | None = None,
    ) -> None:
        self.flush_seconds = flush_seconds
        self.on_flush = on_flush
        self._buffers = {
            "user": _SpeakerBuffer("参加者"),
            "assistant": _SpeakerBuffer("AI"),
        }

    def add(self, speaker: str, delta: str) -> None:
        """届いた断片を追加し、表示タイマーを延長する。"""
        if speaker not in self._buffers or not delta:
            return

        buffer = self._buffers[speaker]
        # 公式仕様どおり、空白や繰り返しを変更せず到着順に連結する。
        buffer.text += delta

        if buffer.flush_task is not None:
            buffer.flush_task.cancel()
        buffer.flush_task = asyncio.create_task(self._flush_later(speaker))

    async def _flush_later(self, speaker: str) -> None:
        try:
            await asyncio.sleep(self.flush_seconds)
            self.flush(speaker)
        except asyncio.CancelledError:
            # 新しい断片が来た場合のキャンセルは正常な動作。
            return

    def flush(self, speaker: str) -> None:
        buffer = self._buffers[speaker]
        text = buffer.text.strip()
        buffer.text = ""
        buffer.flush_task = None
        if text:
            logger.info("%s: %s", buffer.label, text)
            if self.on_flush is not None:
                self.on_flush(speaker, text)

    async def flush_all(self) -> None:
        """通話終了時に、まだ表示していない断片をすべて表示する。"""
        tasks: list[asyncio.Task[None]] = []
        for buffer in self._buffers.values():
            if buffer.flush_task is not None:
                buffer.flush_task.cancel()
                tasks.append(buffer.flush_task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for speaker in self._buffers:
            self.flush(speaker)
