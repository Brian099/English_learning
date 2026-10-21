#!/usr/bin/env python3
"""pip 包装器：绕开沙箱下 tempfile.mkdtemp 目录不可写的问题。

现象：本机沙箱里 tempfile.mkdtemp() 建出来的目录 ACL 不可读不可写
（os.makedirs 建的目录则完全正常），而 pip 内部大量使用 mkdtemp，
于是 pip 必然 PermissionError。这里把 mkdtemp 换成 os.makedirs 实现。

用法：
    python tools/pipwrap.py install --target .asr-libs faster-whisper
    python tools/pipwrap.py download --no-deps -d wheels six
"""
import os
import sys
import tempfile
import uuid


def _mkdtemp(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
    base = dir or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    for _ in range(100):
        name = os.path.join(base, f"{prefix or 'tmp'}{uuid.uuid4().hex[:10]}{suffix or ''}")
        try:
            os.makedirs(name, exist_ok=False)
            return name
        except FileExistsError:
            continue
    raise FileExistsError("无法创建唯一临时目录")


tempfile.mkdtemp = _mkdtemp  # type: ignore[assignment]

from pip._internal.cli.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
