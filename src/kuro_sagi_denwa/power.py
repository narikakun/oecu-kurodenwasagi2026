"""Raspberry Piの電源操作。"""

import subprocess
import sys
from pathlib import Path


def is_raspberry_pi() -> bool:
    if sys.platform != "linux":
        return False
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            if Path(path).read_bytes().rstrip(b"\0").startswith(b"Raspberry Pi"):
                return True
        except OSError:
            continue
    return False


def shutdown_raspberry_pi() -> None:
    if not is_raspberry_pi():
        raise RuntimeError("Raspberry Piでのみ電源を切れます")
    subprocess.run(
        ["sudo", "-n", "/usr/sbin/shutdown", "-h", "now"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
