#!/usr/bin/env python3
"""新增视频一条龙：预处理 → 转写 → 导入服务端。

增量安全：已经处理过的文件会跳过，集号接着已有编号往后排，清单不会被覆盖。

用法：
    # 只处理，不导入（之后自己拷文件）
    python tools/add_videos.py --src "D:\\新素材"

    # 处理完直接导入到 NAS 上的服务
    python tools/add_videos.py --src "D:\\新素材" \
        --server http://192.168.1.10:18080 --password 你的家长密码

    # 只想转写，不重新预处理
    python tools/add_videos.py --src "D:\\新素材" --skip-prep

参数：
    --src          新视频所在目录（可以只有一个系列的新文件）
    --out          处理输出目录，默认 prepared（要和上次的一致）
    --exclude      要排除的系列关键字，默认沿用 prep.py 的
    --server       服务地址；给了才会执行导入
    --password     家长密码
    --skip-prep    跳过预处理（只做转写/导入）
    --skip-transcribe 跳过转写（只做预处理/导入）
    --force        强制重新处理已处理过的文件
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent


def run(step: str, cmd: list[str]) -> int:
    print(f"\n{'=' * 60}\n▶ {step}\n{'=' * 60}")
    result = subprocess.run(cmd, cwd=str(PROJECT))
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="新增视频一条龙")
    parser.add_argument("--src", required=True, help="新视频目录")
    parser.add_argument("--out", default="prepared", help="处理输出目录（默认 prepared）")
    parser.add_argument("--exclude", nargs="*", default=None, help="要排除的系列关键字")
    parser.add_argument("--server", default="", help="服务地址，如 http://192.168.1.10:18080")
    parser.add_argument("--password", default="", help="家长密码")
    parser.add_argument("--skip-prep", action="store_true")
    parser.add_argument("--skip-transcribe", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    src = Path(args.src)
    if not src.is_absolute():
        src = PROJECT / src
    if not src.is_dir():
        print(f"✗ 源目录不存在: {src}")
        return 2

    out = args.out
    out_abs = Path(out) if Path(out).is_absolute() else PROJECT / out
    python = sys.executable

    # 1. 预处理（增量）
    if not args.skip_prep:
        cmd = [python, str(HERE / "prep.py"), "--src", str(src), "--out", str(out_abs)]
        if args.exclude:
            cmd += ["--exclude", *args.exclude]
        if args.force:
            cmd.append("--force")
        if run("1/3 预处理（过滤 / 改名 / 统一响度 / 抽音轨）", cmd) != 0:
            print("\n✗ 预处理有失败项，后续步骤已中止")
            return 1
    else:
        print("· 跳过预处理")

    # 2. 转写（只处理新出现的音频）
    if not args.skip_transcribe:
        cmd = [python, str(HERE / "transcribe.py"),
               "--src", str(out_abs / "asr"),
               "--out", str(out_abs / "transcript"),
               "--skip-existing"]
        if run("2/3 转写（faster-whisper，跳过已转写的）", cmd) != 0:
            print("\n✗ 转写有失败项，继续尝试导入已成功的部分")
    else:
        print("· 跳过转写")

    # 3. 导入服务端
    if args.server:
        if not args.password:
            print("\n✗ 给了 --server 就必须给 --password")
            return 2
        cmd = [python, str(HERE / "import_content.py"),
               "--base", args.server, "--password", args.password,
               "--manifest", str(out_abs / "manifest.json"),
               "--transcript", str(out_abs / "transcript")]
        if run("3/3 导入服务端（清单 + 转写 + 自动分级）", cmd) != 0:
            return 1
    else:
        print("\n· 没有给 --server，跳过导入")

    # 收尾提示
    print(f"\n{'=' * 60}\n下一步\n{'=' * 60}")
    print("1. 把新文件拷到 NAS 的媒体目录（只需拷新增的）：")
    print(f"   {out_abs / 'video'}/*  →  <MEDIA_ROOT>/video/")
    print(f"   {out_abs / 'audio'}/*  →  <MEDIA_ROOT>/audio/")
    print("   用 rsync 增量同步最省事：")
    print(f"   rsync -av --ignore-existing {out_abs}/video/ user@nas:/vol1/1000/media/english-kids/video/")
    print(f"   rsync -av --ignore-existing {out_abs}/audio/ user@nas:/vol1/1000/media/english-kids/audio/")
    print("\n2. 导入（如果上面没给 --server）：")
    print(f"   python tools/import_content.py --base http://<NAS>:18080 --password <密码>")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main())
