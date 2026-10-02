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
    live_voice: str = "marin"
    live_instructions: str = ""
    hook_gpio: int = 17
    dial_gpio: int = 27
    hook_lifted_when_low: bool = False
    dial_pulse_when_low: bool = True
    audio_input_device: str | int | None = None
    audio_output_device: str | int | None = None
    sample_rate: int = 24_000
    block_ms: int = 20
    hook_debounce_ms: int = 30
    dial_debounce_ms: int = 12
    digit_timeout_ms: int = 150
    transcript_flush_ms: int = 800
    mock_gpio: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            live_model=os.getenv("LIVE_MODEL", "gpt-live-1").strip(),
            live_voice=os.getenv("LIVE_VOICE", "marin").strip(),
            live_instructions=os.getenv(
                "LIVE_INSTRUCTIONS",
                "",
            ).strip(),
            hook_gpio=int(os.getenv("HOOK_GPIO", "17")),
            dial_gpio=int(os.getenv("DIAL_GPIO", "27")),
            hook_lifted_when_low=_bool_env("HOOK_LIFTED_WHEN_LOW", False),
            dial_pulse_when_low=_bool_env("DIAL_PULSE_WHEN_LOW", True),
            audio_input_device=_device_env("AUDIO_INPUT_DEVICE"),
            audio_output_device=_device_env("AUDIO_OUTPUT_DEVICE"),
            transcript_flush_ms=int(os.getenv("TRANSCRIPT_FLUSH_MS", "800")),
            mock_gpio=_bool_env("MOCK_GPIO", False),
        )

    def validate(self) -> None:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEYが設定されていません")
        if self.hook_gpio == self.dial_gpio:
            raise ValueError("HOOK_GPIOとDIAL_GPIOには別の番号を指定してください")
        if self.sample_rate <= 0 or self.block_ms <= 0:
            raise ValueError("音声のサンプルレートとブロック時間は正の値が必要です")
        if self.transcript_flush_ms <= 0:
            raise ValueError("TRANSCRIPT_FLUSH_MSは正の値が必要です")
