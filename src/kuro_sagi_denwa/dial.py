"""ロータリーダイヤルのパルスを数字へ変換する。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DialDecoder:
    """パルス数を数え、一定時間の無入力後に一桁を確定する。"""

    debounce_seconds: float = 0.012
    digit_timeout_seconds: float = 0.150
    pulse_count: int = 0
    last_pulse_at: float | None = None

    def add_pulse(self, now: float) -> bool:
        """有効なパルスなら数に加える。戻り値は受理したかどうか。"""
        if self.last_pulse_at is not None:
            if now - self.last_pulse_at < self.debounce_seconds:
                return False

        self.pulse_count += 1
        self.last_pulse_at = now
        return True

    def read_digit(self, now: float) -> int | None:
        """桁間の待ち時間を過ぎていれば数字を返す。"""
        if self.last_pulse_at is None:
            return None
        if now - self.last_pulse_at < self.digit_timeout_seconds:
            return None

        count = self.pulse_count
        self.reset()

        if count == 10:
            return 0
        if 1 <= count <= 9:
            return count
        # 11回以上などはノイズまたは読み取り失敗として捨てる。
        return None

    def reset(self) -> None:
        self.pulse_count = 0
        self.last_pulse_at = None

