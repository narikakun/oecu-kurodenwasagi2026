"""ロータリーダイヤルのパルスを数字へ変換する。"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DialDecoder:
    """パルス数を数え、一定時間の無入力後に一桁を確定する。

    短いチャタリングはgpiozeroのbounce_timeで除き、それを抜けた余分なパルスは
    ダイヤルの周期より明らかに短い間隔かどうかで捨てる。
    """

    min_pulse_interval_seconds: float = 0.025
    digit_timeout_seconds: float = 0.150
    # 実機のダイヤルは指で回し始めた瞬間に余分なパルスを1回出すため、
    # 操作ごとの最初のパルスを捨てる。
    skip_first_pulse: bool = False
    # 最初のパルスを捨てた後、続きが来ないまま経過したら次の操作として扱い直す。
    first_pulse_timeout_seconds: float = 3.0
    pulse_count: int = 0
    last_pulse_at: float | None = None
    last_edge_at: float | None = None
    _skipped_at: float | None = None
    _skipped_edge_at: float | None = None
    _ready: deque[int] = field(default_factory=deque)

    def add_pulse(self, now: float, edge_at: float | None = None) -> None:
        """パルスを数える。前のパルスから桁間の時間が空いていれば前の桁を確定する。

        edge_at があれば、受け取った時刻ではなくエッジの時刻で間隔を測る。
        """
        if self.last_pulse_at is not None:
            interval = self._interval(now, edge_at, self.last_pulse_at, self.last_edge_at)
            # 取り出しが遅れて複数桁のパルスがまとめて届いても、
            # 記録時刻の間隔で桁を分けて合算しないようにする。
            if interval >= self.digit_timeout_seconds:
                self._finish_digit()
            elif interval < self.min_pulse_interval_seconds:
                logger.debug(
                    "ダイヤルパルス間隔が短いためチャタリングとして無視します: %.1f ms",
                    interval * 1000,
                )
                return
            else:
                logger.debug("ダイヤルパルス間隔: %.1f ms", interval * 1000)
        elif self._skipped_at is not None:
            interval = self._interval(now, edge_at, self._skipped_at, self._skipped_edge_at)
            if interval >= self.first_pulse_timeout_seconds:
                # 続きが来なかった場合は、このパルスを新しい操作の最初として扱う。
                self._skipped_at = None
            elif interval < self.min_pulse_interval_seconds:
                logger.debug(
                    "回し始めのパルス直後のチャタリングを無視します: %.1f ms",
                    interval * 1000,
                )
                return

        if self.skip_first_pulse and self._skipped_at is None and self.pulse_count == 0:
            logger.debug("回し始めのパルスとして無視します")
            self._skipped_at = now
            self._skipped_edge_at = edge_at
            return

        self.pulse_count += 1
        self.last_pulse_at = now
        self.last_edge_at = edge_at

    @staticmethod
    def _interval(
        now: float,
        edge_at: float | None,
        previous_at: float,
        previous_edge_at: float | None,
    ) -> float:
        if edge_at is not None and previous_edge_at is not None:
            return edge_at - previous_edge_at
        return now - previous_at

    def read_digit(self, now: float) -> int | None:
        """確定済みの数字があれば1つ返す。"""
        if (
            self.last_pulse_at is not None
            and now - self.last_pulse_at >= self.digit_timeout_seconds
        ):
            self._finish_digit()
        elif (
            self._skipped_at is not None
            and now - self._skipped_at >= self.first_pulse_timeout_seconds
        ):
            logger.warning("回し始めのパルスの後に数字が続かなかったため、待機状態へ戻します")
            self._clear_skipped()
        if self._ready:
            return self._ready.popleft()
        return None

    def _finish_digit(self) -> None:
        count = self.pulse_count
        self.pulse_count = 0
        self.last_pulse_at = None
        self.last_edge_at = None
        self._clear_skipped()

        if count == 10:
            self._ready.append(0)
        elif 1 <= count <= 9:
            self._ready.append(count)
        else:
            # 11回以上などはノイズまたは読み取り失敗として捨てる。
            logger.warning("不正なダイヤルパルス数のため破棄します: %d", count)

    def _clear_skipped(self) -> None:
        self._skipped_at = None
        self._skipped_edge_at = None

    def reset(self) -> None:
        self.pulse_count = 0
        self.last_pulse_at = None
        self.last_edge_at = None
        self._clear_skipped()
        self._ready.clear()
