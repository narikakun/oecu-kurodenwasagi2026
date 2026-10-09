"""USB受話器の録音と再生を扱う。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
from array import array
from pathlib import Path
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)


def _resolve_device(sd: Any, device: str | int | None, name: str | None, kind: str):
    """保持した名前から現在のデバイス番号を引き直す。"""
    if not name:
        return device
    channel_key = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        devices = list(sd.query_devices())
    except Exception:
        return device
    matched = next(
        (
            index
            for index, info in enumerate(devices)
            if info.get("name") == name and info.get(channel_key, 0) > 0
        ),
        None,
    )
    if matched is not None:
        return matched
    # 環境変数にはPortAudioが解決する部分名（pipewireなど）も指定できる。
    try:
        sd.query_devices(name, kind=kind)
        return name
    except Exception:
        return None


def _supported_sample_rate(
    sd: Any, device: str | int | None, kind: str, configured_rate: int
) -> int:
    """設定レートを確認し、非対応なら機器の既定レートを使う。"""
    checker = getattr(sd, f"check_{kind}_settings", None)
    if checker is None:
        return configured_rate
    try:
        checker(
            device=device,
            channels=1,
            dtype="int16",
            samplerate=configured_rate,
        )
        return configured_rate
    except Exception as configured_error:
        try:
            info = sd.query_devices(device, kind=kind)
            default_rate = round(float(info["default_samplerate"]))
            checker(
                device=device,
                channels=1,
                dtype="int16",
                samplerate=default_rate,
            )
        except Exception:
            raise configured_error
        logger.warning(
            "%sデバイスは%d Hz非対応のため%d Hzを使います",
            kind,
            configured_rate,
            default_rate,
        )
        return default_rate


class AudienceOutput:
    """受話器音声とベル音を複製する観客用出力設定。"""

    def __init__(self, settings: Settings) -> None:
        self.output_device = settings.audience_output_device
        self.device_name = (
            settings.audience_output_device
            if isinstance(settings.audience_output_device, str)
            else None
        )
        self.sample_rate = settings.audience_device_sample_rate
        self.volume = settings.audience_volume


class BellRinger:
    """受話器とは別の出力デバイスで黒電話のベル音を繰り返す。"""

    def __init__(
        self,
        settings: Settings,
        audience: AudienceOutput | None = None,
        path: str | Path | None = None,
    ) -> None:
        self.settings = settings
        self.path = Path(path) if path else (
            Path(__file__).parent / "assets" / "audio" / "Rotary_Phone-Ringtone01-1.mp3"
        )
        self.output_device = settings.bell_output_device
        self.device_name = (
            settings.bell_output_device
            if isinstance(settings.bell_output_device, str)
            else None
        )
        self.sample_rate = settings.bell_device_sample_rate
        self.volume = settings.bell_volume
        self.audience = audience
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

        bell_device = _resolve_device(
            sd, self.output_device, self.device_name, "output"
        )
        audience_device = None
        if self.audience is not None:
            audience_device = _resolve_device(
                sd,
                self.audience.output_device,
                self.audience.device_name,
                "output",
            )

        stream = None
        if bell_device is not None or self.device_name is None:
            try:
                self.sample_rate = _supported_sample_rate(
                    sd, bell_device, "output", self.sample_rate
                )
                stream = sd.RawOutputStream(
                    samplerate=self.sample_rate,
                    blocksize=self.sample_rate * self.settings.block_ms // 1000,
                    channels=1,
                    dtype="int16",
                    device=bell_device,
                )
            except Exception as error:
                logger.warning("ベル用出力を使用できません: %s", error)
        audience_stream = None
        if self.audience is not None and audience_device is not None:
            try:
                self.audience.sample_rate = _supported_sample_rate(
                    sd, audience_device, "output", self.audience.sample_rate
                )
                audience_stream = sd.RawOutputStream(
                    samplerate=self.audience.sample_rate,
                    blocksize=self.audience.sample_rate
                    * self.settings.block_ms
                    // 1000,
                    channels=1,
                    dtype="int16",
                    device=audience_device,
                )
            except Exception as error:
                logger.warning("観客用出力を無効化します: %s", error)
        if stream is None and audience_stream is None:
            logger.error("利用できるベル出力がありません")
            return

        source_rate = (
            self.sample_rate
            if stream is not None
            else self.audience.sample_rate
        )
        sound = await asyncio.to_thread(
            miniaudio.decode_file,
            str(self.path),
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=1,
            sample_rate=source_rate,
        )
        # 1サンプルずつの処理は重いため、GPIO監視を止めないよう別スレッドで行う。
        pcm = await asyncio.to_thread(scale_pcm16, sound.samples, self.volume)
        pcm = resample_pcm16_mono(pcm, source_rate, self.sample_rate)
        audience_pcm = None
        if audience_stream is not None and self.audience is not None:
            audience_pcm = await asyncio.to_thread(
                scale_pcm16, sound.samples, self.audience.volume
            )
            audience_pcm = resample_pcm16_mono(
                audience_pcm, source_rate, self.audience.sample_rate
            )
        source_bytes_per_block = source_rate * self.settings.block_ms // 1000 * 2
        bytes_per_block = self.sample_rate * self.settings.block_ms // 1000 * 2
        audience_bytes_per_block = (
            self.audience.sample_rate * self.settings.block_ms // 1000 * 2
            if self.audience is not None
            else 0
        )
        stop_event = threading.Event()

        def play() -> None:
            nonlocal stream, audience_stream

            def close_stream(target) -> None:
                if target is None:
                    return
                try:
                    target.abort()
                except Exception:
                    pass
                try:
                    target.close()
                except Exception:
                    pass

            # ALSAのストリームは複数スレッドから同時に操作すると内部状態が壊れるため、
            # 開始・書き込み・停止をすべてこのスレッドだけで行う。
            try:
                if stream is not None:
                    try:
                        stream.start()
                    except Exception as error:
                        logger.warning("ベル用出力を開始できません: %s", error)
                        close_stream(stream)
                        stream = None
                if audience_stream is not None:
                    try:
                        audience_stream.start()
                    except Exception as error:
                        logger.warning("観客用出力を開始できません: %s", error)
                        close_stream(audience_stream)
                        audience_stream = None
                if stream is None and audience_stream is None:
                    return
                logger.info("ベルを鳴らします")
                while not stop_event.is_set():
                    for block_index, offset in enumerate(
                        range(0, len(sound.samples) * 2, source_bytes_per_block)
                    ):
                        if stop_event.is_set():
                            break
                        if stream is not None:
                            bell_offset = block_index * bytes_per_block
                            try:
                                stream.write(
                                    pcm[bell_offset : bell_offset + bytes_per_block]
                                )
                            except Exception as error:
                                logger.warning("ベル用出力が切断されました: %s", error)
                                close_stream(stream)
                                stream = None
                        if audience_stream is not None and audience_pcm is not None:
                            audience_offset = block_index * audience_bytes_per_block
                            try:
                                audience_stream.write(
                                    audience_pcm[
                                        audience_offset : audience_offset
                                        + audience_bytes_per_block
                                    ]
                                )
                            except Exception as error:
                                logger.warning("観客用出力が切断されました: %s", error)
                                close_stream(audience_stream)
                                audience_stream = None
                        if stream is None and audience_stream is None:
                            return
            finally:
                close_stream(stream)
                close_stream(audience_stream)
                logger.info("ベルを停止しました")

        player = asyncio.ensure_future(asyncio.to_thread(play))
        try:
            await asyncio.shield(player)
        except asyncio.CancelledError:
            stop_event.set()
            # 書き込み中のブロックが終わり、ストリームを閉じるまで待つ。
            await asyncio.gather(player, return_exceptions=True)
            raise


class AudioDeviceSelector:
    """ロータリーダイヤルで実行中の音声デバイスを選択する。"""

    TARGETS = {
        0: ("受話器入力", "input"),
        1: ("受話器出力", "output"),
        2: ("ベル出力", "bell"),
        3: ("観客用出力", "audience"),
    }
    VOLUME_TARGETS = {
        4: ("受話器音量", "output"),
        5: ("ベル音量", "bell"),
        6: ("観客用音量", "audience"),
    }

    def __init__(
        self,
        audio: AudioDevice,
        ringer: BellRinger,
        display: Any = None,
        audience: AudienceOutput | None = None,
        settings_path: str | Path | None = None,
    ) -> None:
        self.audio = audio
        self.ringer = ringer
        self.audience = audience or ringer.audience or AudienceOutput(audio.settings)
        self.audio.audience = self.audience
        self.ringer.audience = self.audience
        self.display = display
        self.settings_path = Path(
            settings_path or audio.settings.audio_settings_file
        ).expanduser()
        self.active = False
        self._pending_target: str | None = None
        self._pending_kind: str | None = None

    def restore(self) -> None:
        """保存したデバイスを復元し、見つからない項目はOS既定へ戻す。"""
        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError, TypeError) as error:
            logger.warning("音声デバイス設定を読み込めませんでした: %s", error)
            return

        try:
            devices = self._devices()
        except Exception as error:
            logger.warning("音声デバイス一覧を取得できないため保存設定を復元しません: %s", error)
            return
        for target in ("input", "output", "bell", "audience"):
            name = saved.get(target)
            if target == "audience" and not name:
                self.audience.output_device = None
                continue
            index = self._find_device(devices, name, target)
            if index is None:
                logger.warning(
                    "保存した%sデバイスが見つからないためOSの既定へ戻します: %r",
                    self._target_label(target),
                    name,
                )
                self._apply_default(target)
            else:
                self._apply_device(target, index, self._device_sample_rate(devices[index]))
        self.audio.output_volume = self._saved_volume(
            saved, "output_volume", self.audio.output_volume
        )
        self.ringer.volume = self._saved_volume(
            saved, "bell_volume", self.ringer.volume
        )
        self.audience.volume = self._saved_volume(
            saved, "audience_volume", self.audience.volume
        )
        self._save()

    def show_current(self) -> None:
        input_description, output_description, bell_description, audience_description = (
            self._current_descriptions()
        )
        logger.info(
            "音声設定: 受話器入力=%s / 受話器出力=%s / ベル出力=%s / 観客用出力=%s",
            input_description,
            output_description,
            bell_description,
            audience_description,
        )
        self._show_on_display(
            "現在の音声設定\n"
            f"受話器入力：{input_description}\n"
            f"受話器出力：{output_description}（{self._volume_percent(self.audio.output_volume)}%）\n"
            f"ベル出力：{bell_description}（{self._volume_percent(self.ringer.volume)}%）\n"
            f"観客用出力：{audience_description}（{self._volume_percent(self.audience.volume)}%）"
        )

    def enter(self) -> None:
        self.active = True
        self._pending_target = None
        self._pending_kind = None
        logger.info(
            "音声設定モード: 0=受話器入力, 1=受話器出力, 2=ベル出力, 3=観客用出力, 4〜6=音量"
        )
        self._show_menu()

    def exit(self) -> None:
        if self.active:
            logger.info("音声デバイス設定モードを終了します")
        self.active = False
        self._pending_target = None
        self._pending_kind = None
        if self.display is not None:
            self.display.publish({"type": "settings", "active": False})

    def handle_digit(self, digit: int) -> None:
        if self._pending_target is None:
            target = self.TARGETS.get(digit)
            if target is not None:
                label, self._pending_target = target
                self._pending_kind = "device"
                logger.info("%sを選択します。次にデバイス番号を回してください", label)
                self._show_available(self._pending_target)
                return
            volume_target = self.VOLUME_TARGETS.get(digit)
            if volume_target is not None:
                label, self._pending_target = volume_target
                self._pending_kind = "volume"
                logger.info("%sを選択します。0（消音）〜9（最大）を回してください", label)
                self._show_volume_picker(label)
                return
            logger.warning("0から6のいずれかを回してください")
            return

        if self._pending_kind == "volume":
            self._apply_volume(self._pending_target, digit / 9)
            self._save()
            logger.info(
                "%sを%d%%へ変更しました",
                self._target_label(self._pending_target),
                self._volume_percent(digit / 9),
            )
            self._pending_target = None
            self._pending_kind = None
            self._show_menu()
            return

        if not self._supports(digit, self._pending_target):
            logger.warning("デバイス%dは選択した用途では使用できません", digit)
            self._show_available(self._pending_target)
            return

        label = next(
            name for name, target in self.TARGETS.values() if target == self._pending_target
        )
        sample_rate = self._default_sample_rate(digit)
        self._apply_device(self._pending_target, digit, sample_rate)
        self._save()
        logger.info(
            "%sをデバイス%d（%d Hz）へ変更しました",
            label,
            digit,
            sample_rate,
        )
        self._pending_target = None
        self._pending_kind = None
        self._show_menu()
        logger.info("続けて0から6を回すか、受話器を上げて設定を終了してください")

    def _show_menu(self) -> None:
        input_description, output_description, bell_description, audience_description = (
            self._current_descriptions()
        )
        self._show_on_display(
            "音声デバイス設定\n"
            f"現在の受話器入力：{input_description} / {self.audio.input_sample_rate} Hz\n"
            f"現在の受話器出力：{output_description} / {self.audio.output_sample_rate} Hz\n"
            f"現在のベル出力：{bell_description} / {self.ringer.sample_rate} Hz\n\n"
            f"現在の観客用出力：{audience_description} / {self.audience.sample_rate} Hz\n"
            f"音量：受話器 {self._volume_percent(self.audio.output_volume)}% / "
            f"ベル {self._volume_percent(self.ringer.volume)}% / "
            f"観客 {self._volume_percent(self.audience.volume)}%\n\n"
            "変更する項目をダイヤルしてください\n"
            "0：受話器入力　1：受話器出力　2：ベル出力　3：観客用出力\n"
            "4：受話器音量　5：ベル音量　6：観客用音量"
        )

    def _current_descriptions(self) -> tuple[str, str, str, str]:
        return (
            self._describe(self.audio.input_device, "input"),
            self._describe(self.audio.output_device, "output"),
            self._describe(self.ringer.output_device, "output"),
            self._describe(self.audience.output_device, "output")
            if self.audience.output_device is not None
            else "未設定（無効）",
        )

    def _show_volume_picker(self, label: str) -> None:
        self._show_on_display(
            f"{label}\n\n0：消音　1〜8：中間　9：最大\n"
            "設定する音量をダイヤルしてください"
        )

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
        self._show_on_display("選択可能なデバイス\n" + ("\n".join(available) or "なし"))

    def _show_on_display(self, text: str) -> None:
        if self.display is not None:
            self.display.publish({"type": "settings", "active": True, "text": text})

    def _supports(self, index: int, target: str) -> bool:
        devices = self._devices()
        if not 0 <= index < len(devices):
            return False
        channel_key = "max_input_channels" if target == "input" else "max_output_channels"
        return devices[index][channel_key] > 0

    def _default_sample_rate(self, index: int) -> int:
        return self._device_sample_rate(self._devices()[index])

    @staticmethod
    def _device_sample_rate(device: dict[str, Any]) -> int:
        return round(float(device["default_samplerate"]))

    @staticmethod
    def _target_label(target: str) -> str:
        return {
            "input": "受話器入力",
            "output": "受話器出力",
            "bell": "ベル出力",
            "audience": "観客用出力",
        }[target]

    @staticmethod
    def _find_device(
        devices: list[dict[str, Any]], name: Any, target: str
    ) -> int | None:
        if not isinstance(name, str) or not name:
            return None
        channel_key = "max_input_channels" if target == "input" else "max_output_channels"
        return next(
            (
                index
                for index, device in enumerate(devices)
                if device.get("name") == name and device.get(channel_key, 0) > 0
            ),
            None,
        )

    def _apply_device(self, target: str, device: int | None, sample_rate: int) -> None:
        device_name = None
        if device is not None:
            try:
                device_name = str(self._devices()[device]["name"])
            except (IndexError, KeyError, TypeError):
                pass
        if target == "input":
            self.audio.input_device = device
            self.audio.input_device_name = device_name
            self.audio.input_sample_rate = sample_rate
        elif target == "output":
            self.audio.output_device = device
            self.audio.output_device_name = device_name
            self.audio.output_sample_rate = sample_rate
        elif target == "bell":
            self.ringer.output_device = device
            self.ringer.device_name = device_name
            self.ringer.sample_rate = sample_rate
        else:
            self.audience.output_device = device
            self.audience.device_name = device_name
            self.audience.sample_rate = sample_rate

    def remember_current_names(self) -> None:
        """起動時の番号設定を、抜き差しに強いデバイス名として保持する。"""
        try:
            devices = self._devices()
        except Exception as error:
            logger.warning("音声デバイス名を記憶できませんでした: %s", error)
            return
        for target, device in (
            ("input", self.audio.input_device),
            ("output", self.audio.output_device),
            ("bell", self.ringer.output_device),
            ("audience", self.audience.output_device),
        ):
            if isinstance(device, int) and 0 <= device < len(devices):
                name = str(devices[device].get("name", "")) or None
                if target == "input":
                    self.audio.input_device_name = name
                elif target == "output":
                    self.audio.output_device_name = name
                elif target == "bell":
                    self.ringer.device_name = name
                else:
                    self.audience.device_name = name

    def _apply_default(self, target: str) -> None:
        import sounddevice as sd

        kind = "input" if target == "input" else "output"
        try:
            info = sd.query_devices(kind=kind)
            sample_rate = self._device_sample_rate(info)
        except Exception as error:
            logger.warning(
                "%sの既定サンプルレートを取得できないため設定値を使います: %s",
                self._target_label(target),
                error,
            )
            sample_rate = (
                self.audio.input_sample_rate
                if target == "input"
                else self.audio.output_sample_rate
                if target == "output"
                else self.ringer.sample_rate
                if target == "bell"
                else self.audience.sample_rate
            )
        self._apply_device(target, None, sample_rate)

    def _selected_name(self, target: str) -> str | None:
        import sounddevice as sd

        device = (
            self.audio.input_device
            if target == "input"
            else self.audio.output_device
            if target == "output"
            else self.ringer.output_device
            if target == "bell"
            else self.audience.output_device
        )
        if target == "audience" and device is None:
            return None
        kind = "input" if target == "input" else "output"
        try:
            return str(sd.query_devices(device, kind=kind)["name"])
        except Exception as error:
            logger.warning("%sの名前を取得できませんでした: %s", self._target_label(target), error)
            return None

    def _save(self) -> None:
        data = {
            target: self._selected_name(target)
            for target in ("input", "output", "bell", "audience")
        }
        data.update(
            {
                "output_volume": self.audio.output_volume,
                "bell_volume": self.ringer.volume,
                "audience_volume": self.audience.volume,
            }
        )
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.settings_path.with_suffix(self.settings_path.suffix + ".tmp")
            temporary_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.settings_path)
        except OSError as error:
            logger.warning("音声デバイス設定を保存できませんでした: %s", error)

    def _apply_volume(self, target: str, volume: float) -> None:
        if target == "output":
            self.audio.output_volume = volume
        elif target == "bell":
            self.ringer.volume = volume
        else:
            self.audience.volume = volume

    @staticmethod
    def _saved_volume(saved: dict[str, Any], key: str, default: float) -> float:
        value = saved.get(key, default)
        if isinstance(value, (int, float)) and 0 <= value <= 1:
            return float(value)
        return default

    @staticmethod
    def _volume_percent(volume: float) -> int:
        return round(volume * 100)

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


def scale_pcm16(samples, volume: float) -> bytes:
    """PCM16のサンプル列へ音量を掛け、範囲外を切り詰める。"""
    scaled = array("h", samples)
    for index, sample in enumerate(scaled):
        scaled[index] = max(-32768, min(32767, int(sample * volume)))
    return scaled.tobytes()


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
    def __init__(
        self, settings: Settings, audience: AudienceOutput | None = None
    ) -> None:
        self.settings = settings
        self.input_device = settings.audio_input_device
        self.output_device = settings.audio_output_device
        self.input_device_name = (
            settings.audio_input_device
            if isinstance(settings.audio_input_device, str)
            else None
        )
        self.output_device_name = (
            settings.audio_output_device
            if isinstance(settings.audio_output_device, str)
            else None
        )
        self.input_sample_rate = settings.audio_device_sample_rate
        self.output_sample_rate = settings.audio_device_sample_rate
        self.output_volume = settings.audio_output_volume
        self.audience = audience
        self.input_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self.output_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=200)
        self._input_stream: Any = None
        self._output_stream: Any = None
        self._audience_stream: Any = None
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
            self.input_sample_rate,
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
        input_device = _resolve_device(
            sd, self.input_device, self.input_device_name, "input"
        )
        output_device = _resolve_device(
            sd, self.output_device, self.output_device_name, "output"
        )
        if self.input_device_name and input_device is None:
            raise RuntimeError(
                f"受話器入力が接続されていません: {self.input_device_name}"
            )
        if self.output_device_name and output_device is None:
            raise RuntimeError(
                f"受話器出力が接続されていません: {self.output_device_name}"
            )
        self.input_sample_rate = _supported_sample_rate(
            sd, input_device, "input", self.input_sample_rate
        )
        self.output_sample_rate = _supported_sample_rate(
            sd, output_device, "output", self.output_sample_rate
        )
        input_frames = self.input_sample_rate * self.settings.block_ms // 1000
        output_frames = self.output_sample_rate * self.settings.block_ms // 1000

        self._input_stream = sd.RawInputStream(
            samplerate=self.input_sample_rate,
            blocksize=input_frames,
            channels=1,
            dtype="int16",
            device=input_device,
            callback=self._capture_callback,
        )
        self._output_stream = sd.RawOutputStream(
            samplerate=self.output_sample_rate,
            blocksize=output_frames,
            channels=1,
            dtype="int16",
            device=output_device,
        )
        if self.audience is not None and self.audience.output_device is not None:
            audience_device = _resolve_device(
                sd,
                self.audience.output_device,
                self.audience.device_name,
                "output",
            )
            if audience_device is None:
                logger.warning(
                    "観客用出力が接続されていないため無効化します: %s",
                    self.audience.device_name,
                )
            else:
                try:
                    self.audience.sample_rate = _supported_sample_rate(
                        sd,
                        audience_device,
                        "output",
                        self.audience.sample_rate,
                    )
                    self._audience_stream = sd.RawOutputStream(
                        samplerate=self.audience.sample_rate,
                        blocksize=self.audience.sample_rate
                        * self.settings.block_ms
                        // 1000,
                        channels=1,
                        dtype="int16",
                        device=audience_device,
                    )
                except Exception as error:
                    logger.warning("観客用出力を無効化します: %s", error)
        self._input_stream.start()
        self._output_stream.start()
        if self._audience_stream is not None:
            try:
                self._audience_stream.start()
            except Exception as error:
                logger.warning("観客用出力を開始できないため無効化します: %s", error)
                await self._close_output_stream("_audience_stream")
        self._play_task = asyncio.create_task(self._play_loop())
        logger.info("USB受話器の録音・再生を開始しました")

    async def _play_loop(self) -> None:
        while True:
            data = await self.output_queue.get()
            if data is None:
                self.output_queue.task_done()
                return
            if self._output_stream is not None or self._audience_stream is not None:
                writes = []
                targets = []
                if self._output_stream is not None:
                    device_data = resample_pcm16_mono(
                        scale_pcm16(data, self.output_volume),
                        self.settings.sample_rate,
                        self.output_sample_rate,
                    )
                    writes.append(
                        asyncio.to_thread(self._output_stream.write, device_data)
                    )
                    targets.append("output")
                if self._audience_stream is not None and self.audience is not None:
                    audience_data = resample_pcm16_mono(
                        scale_pcm16(data, self.audience.volume),
                        self.settings.sample_rate,
                        self.audience.sample_rate,
                    )
                    writes.append(
                        asyncio.to_thread(self._audience_stream.write, audience_data)
                    )
                    targets.append("audience")
                results = await asyncio.gather(*writes, return_exceptions=True)
                for target, result in zip(targets, results):
                    if not isinstance(result, Exception):
                        continue
                    if target == "output":
                        logger.warning("受話器出力が切断されました: %s", result)
                        await self._close_output_stream("_output_stream")
                    else:
                        logger.warning("観客用出力が切断されました: %s", result)
                        await self._close_output_stream("_audience_stream")
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

    async def _close_output_stream(self, attribute: str) -> None:
        stream = getattr(self, attribute)
        if stream is None:
            return
        for action in (stream.abort, stream.close):
            try:
                await asyncio.to_thread(action)
            except Exception:
                pass
        setattr(self, attribute, None)

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
        pcm = await asyncio.to_thread(scale_pcm16, sound.samples, volume)

        self._hold_task = asyncio.create_task(self._hold_music_loop(pcm))
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

        for stream in (self._input_stream, self._output_stream, self._audience_stream):
            if stream is not None:
                try:
                    await asyncio.to_thread(stream.stop)
                except Exception:
                    pass
                try:
                    await asyncio.to_thread(stream.close)
                except Exception:
                    pass

        self._input_stream = None
        self._output_stream = None
        self._audience_stream = None
        self._loop = None

        while not self.input_queue.empty():
            self.input_queue.get_nowait()
        logger.info("USB受話器を停止しました")
