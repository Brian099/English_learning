#!/usr/bin/env python3
"""把本地处理好的内容导入到运行中的服务端。

在 Windows 上跑完 `tools/prep.py` 和 `tools/transcribe.py` 之后，用这个脚本
把清单和转写推到 NAS 上的服务里。也可以对本地开发服务用。

用法：
    python tools/import_content.py --base http://192.168.1.10:18080 --password 你的密码
    python tools/import_content.py --base ... --password ... --student 小明 --skip-transcript

前置：
    prepared/manifest.json      ← tools/prep.py 产出
    prepared/transcript/*.json  ← tools/transcribe.py 产出（可选）

注意：视频和音轨文件本身要先拷到 NAS 的 MEDIA_ROOT 下（manifest 里的
video_out / audio_out 是相对路径），本脚本只导入元数据与转写。
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def call(base: str, path: str, method: str = "GET", body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read().decode()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="导入预处理清单与转写到服务端")
    parser.add_argument("--base", required=True, help="服务地址，如 http://192.168.1.10:18080")
    parser.add_argument("--password", required=True, help="家长密码")
    parser.add_argument("--manifest", default=str(PROJECT / "prepared" / "manifest.json"))
    parser.add_argument("--transcript", default=str(PROJECT / "prepared" / "transcript"))
    parser.add_argument("--student", default="", help="没有学生时自动创建的名字")
    parser.add_argument("--skip-manifest", action="store_true")
    parser.add_argument("--skip-transcript", action="store_true")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    token = call(base, "/api/admin/login", "POST", {"password": args.password})["token"]
    print(f"✓ 登录成功：{base}")

    students = call(base, "/api/students", token=token)
    if not students:
        name = args.student or "小朋友"
        created = call(base, "/api/students", "POST",
                       {"name": name, "daily_target_min": 20}, token)
        print(f"✓ 创建学生：{created['name']}")
    else:
        print(f"✓ 已有学生：{', '.join(s['name'] for s in students)}")

    manifest_path = Path(args.manifest)
    if args.skip_manifest:
        print("· 跳过清单导入")
        manifest = {"records": []}
    else:
        if not manifest_path.is_file():
            print(f"✗ 找不到清单：{manifest_path}")
            return 2
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result = call(base, "/api/import/manifest", "POST",
                      {"records": manifest["records"], "source": manifest.get("source")}, token)
        print(f"✓ 导入清单：新增 {result['created']}，更新 {result['updated']}，跳过 {result['skipped']}")

    if args.skip_transcript:
        print("· 跳过转写导入")
        return 0

    # 转写文件名 = 视频文件名（stem），靠清单里的 video_out 映射回标题
    title_by_stem: dict[str, str] = {}
    for record in manifest["records"]:
        if record.get("video_out"):
            title_by_stem[Path(record["video_out"]).stem] = record["title"]

    videos = call(base, "/api/videos?limit=500", token=token)["items"]
    id_by_title = {video["title"]: video["id"] for video in videos}

    transcript_dir = Path(args.transcript)
    if not transcript_dir.is_dir():
        print(f"· 没有转写目录，跳过：{transcript_dir}")
        return 0

    pushed = missing = 0
    for json_path in sorted(transcript_dir.glob("*.json")):
        data = json.loads(json_path.read_text(encoding="utf-8"))
        title = title_by_stem.get(json_path.stem)
        video_id = id_by_title.get(title)
        if not video_id:
            missing += 1
            print(f"  [跳过] 服务端没有这个视频：{json_path.name}")
            continue
        result = call(base, f"/api/videos/{video_id}/transcript", "POST", {
            "model": data.get("model", "small.en"),
            "language": data.get("language", "en"),
            "text": data.get("text", ""),
            "segments": data.get("segments"),
            "speech_ms": int(round(data.get("speech_s", 0) * 1000)),
        }, token)
        metrics = result["metrics"]
        print(f"  {title}: {metrics['level']} · 生词率 {metrics['oov_ratio']:.0%} · "
              f"唯一词 {metrics['unique_words']} · {metrics['wpm']:.0f} WPM")
        pushed += 1

    print(f"\n✓ 完成：{pushed} 集转写入库" + (f"，{missing} 集未匹配" if missing else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
