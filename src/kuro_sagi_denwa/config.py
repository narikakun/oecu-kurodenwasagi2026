"""環境変数からアプリケーション設定を読み込む。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: str | Path = ".env") -> bool:
    """`.env`を読み込み、未設定の環境変数だけを補う。

    大学制作で扱いやすいよう、外部ライブラリを増やさずに一般的な
    `名前=値`、コメント、シングル・ダブルクォートへ対応する。
    """
    env_path = Path(path)
    if not env_path.is_file():
        return False

    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{env_path}:{line_number}: NAME=VALUE形式ではありません")

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
            raise ValueError(f"{env_path}:{line_number}: 環境変数名が不正です")

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        # シェルで明示した値を.envで上書きしない。
        os.environ.setdefault(name, value)

    return True


def _bool_env(name: str, default: bool) -> bool:
    """true/false系の環境変数を読みやすい形で扱う。"""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _device_env(name: str) -> str | int | None:
    """sounddeviceのデバイス名または番号を読み取る。"""
    value = os.getenv(name, "").strip()
    if not value:
        return None
    return int(value) if value.isdecimal() else value


@dataclass(frozen=True)
class Settings:
    api_key: str
    live_model: str = "gpt-live-1"
    live_backend_model: str = "gpt-6-luna"
    live_web_search: bool = True
    live_voice: str = "meridian"
    live_instructions: str = ""
    display_kiosk: bool = True
    display_kiosk_browser: str = ""
    hook_gpio: int = 17
    dial_gpio: int = 26
    hook_lifted_when_low: bool = False
    audio_input_device: str | int | None = None
    audio_output_device: str | int | None = None
    audio_device_sample_rate: int = 48_000
    audio_settings_file: str = "~/.config/kuro-sagi-denwa/audio-settings.json"
    bell_output_device: str | int | None = None
    bell_device_sample_rate: int = 48_000
    bell_volume: float = 0.7
    sample_rate: int = 24_000
    block_ms: int = 20
    hook_debounce_ms: int = 30
    dial_debounce_ms: int = 5
    # 600-A2型は20パルス/秒（約50ms周期）なので、これより短い間隔はチャタリングとみなす。
    dial_min_pulse_interval_ms: int = 25
    # 実機のダイヤルは回し始めに余分なパルスを1回出すため、操作ごとの最初のパルスを捨てる。
    dial_skip_first_pulse: bool = True
    digit_timeout_ms: int = 150
    mock_gpio: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            live_model=os.getenv("LIVE_MODEL", "gpt-live-1").strip(),
            live_backend_model=os.getenv(
                "LIVE_BACKEND_MODEL", "gpt-6-luna"
            ).strip(),
            live_web_search=_bool_env("LIVE_WEB_SEARCH", True),
            live_voice=os.getenv("LIVE_VOICE", "meridian").strip(),
            live_instructions=os.getenv(
                "LIVE_INSTRUCTIONS",
                "",
            ).strip(),
            display_kiosk=_bool_env("DISPLAY_KIOSK", True),
            display_kiosk_browser=os.getenv("DISPLAY_KIOSK_BROWSER", "").strip(),
            hook_gpio=int(os.getenv("HOOK_GPIO", "17")),
            dial_gpio=int(os.getenv("DIAL_GPIO", "26")),
            hook_lifted_when_low=_bool_env("HOOK_LIFTED_WHEN_LOW", False),
            audio_input_device=_device_env("AUDIO_INPUT_DEVICE"),
            audio_output_device=_device_env("AUDIO_OUTPUT_DEVICE"),
            audio_device_sample_rate=int(
                os.getenv("AUDIO_DEVICE_SAMPLE_RATE", "48000")
            ),
            audio_settings_file=os.getenv(
                "AUDIO_SETTINGS_FILE",
                "~/.config/kuro-sagi-denwa/audio-settings.json",
            ).strip(),
            bell_output_device=_device_env("BELL_OUTPUT_DEVICE"),
            bell_device_sample_rate=int(
                os.getenv("BELL_DEVICE_SAMPLE_RATE", "48000")
            ),
            bell_volume=float(os.getenv("BELL_VOLUME", "0.7")),
            dial_min_pulse_interval_ms=int(
                os.getenv("DIAL_MIN_PULSE_INTERVAL_MS", "25")
            ),
            dial_skip_first_pulse=_bool_env("DIAL_SKIP_FIRST_PULSE", True),
            mock_gpio=_bool_env("MOCK_GPIO", False),
        )

    def validate(self) -> None:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEYが設定されていません")
        if self.hook_gpio == self.dial_gpio:
            raise ValueError("HOOK_GPIOとDIAL_GPIOには別の番号を指定してください")
        if (
            self.sample_rate <= 0
            or self.audio_device_sample_rate <= 0
            or self.bell_device_sample_rate <= 0
            or self.block_ms <= 0
        ):
            raise ValueError("音声のサンプルレートとブロック時間は正の値が必要です")
        if not 0 <= self.bell_volume <= 1:
            raise ValueError("BELL_VOLUMEは0から1の間で指定してください")
