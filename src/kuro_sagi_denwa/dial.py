"""ロータリーダイヤルのパルスを数字へ変換する。"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DialDecoder:
    """パルス数を数え、一定時間の無入力後に一桁を確定する。

    チャタリング除去はgpiozeroのbounce_timeに任せ、ここでは数えるだけにする。
    """

    digit_timeout_seconds: float = 0.150
    pulse_count: int = 0
    last_pulse_at: float | None = None
    _ready: deque[int] = field(default_factory=deque)

    def add_pulse(self, now: float) -> None:
        """パルスを数える。前のパルスから桁間の時間が空いていれば前の桁を確定する。"""
        if self.last_pulse_at is not None:
            interval = now - self.last_pulse_at
            # 取り出しが遅れて複数桁のパルスがまとめて届いても、
            # 記録時刻の間隔で桁を分けて合算しないようにする。
            if interval >= self.digit_timeout_seconds:
                self._finish_digit()
            else:
                logger.debug("ダイヤルパルス間隔: %.1f ms", interval * 1000)

        self.pulse_count += 1
        self.last_pulse_at = now

    def read_digit(self, now: float) -> int | None:
        """確定済みの数字があれば1つ返す。"""
        if (
            self.last_pulse_at is not None
            and now - self.last_pulse_at >= self.digit_timeout_seconds
        ):
            self._finish_digit()
        if self._ready:
            return self._ready.popleft()
        return None

    def _finish_digit(self) -> None:
        count = self.pulse_count
        self.pulse_count = 0
        self.last_pulse_at = None

        if count == 10:
            self._ready.append(0)
        elif 1 <= count <= 9:
            self._ready.append(count)
        else:
            # 11回以上などはノイズまたは読み取り失敗として捨てる。
            logger.warning("不正なダイヤルパルス数のため破棄します: %d", count)

    def reset(self) -> None:
        self.pulse_count = 0
        self.last_pulse_at = None
        self._ready.clear()
