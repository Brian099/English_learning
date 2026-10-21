"""ffmpeg / ffprobe 的可执行文件定位。

容器里它们就在 PATH（/usr/bin/ffprobe）；Windows 开发机上可能在 D:\\ffmpeg\\bin
但没进 PATH。统一在这里找，避免各处硬编码。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

_WINDOWS_FALLBACKS = [Path(r"D:\ffmpeg\bin"), Path(r"C:\ffmpeg\bin")]


def _find(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32":
        exe = name + ".exe"
        for base in _WINDOWS_FALLBACKS:
            candidate = base / exe
            if candidate.is_file():
                return str(candidate)
    return None


def ffprobe() -> str | None:
    return _find("ffprobe")


def ffmpeg() -> str | None:
    return _find("ffmpeg")
