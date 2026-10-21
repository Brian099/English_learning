#!/usr/bin/env python3
"""本地开发用：把 prepared/ 的清单和转写灌进后端，方便验证。

前置：后端已在跑，且 DATABASE_URL 指向本地库。

用法：
    python tools/seed_dev.py --base http://127.0.0.1:8010 --password dev
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
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read().decode()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise RuntimeError(f"{method} {path} -> {exc.code}: {detail[:300]}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--password", default="dev")
    parser.add_argument("--manifest", default=str(PROJECT / "prepared" / "manifest.json"))
    parser.add_argument("--transcript", default=str(PROJECT / "prepared" / "transcript"))
    parser.add_argument("--student", default="小明")
    args = parser.parse_args()

    token = call(args.base, "/api/admin/login", "POST", {"password": args.password})["token"]
    print("登录成功")

    students = call(args.base, "/api/students", token=token)
    if not students:
        created = call(args.base, "/api/students", "POST",
                       {"name": args.student, "daily_target_min": 20}, token)
        print(f"创建学生: {created['name']}")
    else:
        print(f"已有学生: {', '.join(s['name'] for s in students)}")

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    result = call(args.base, "/api/import/manifest", "POST",
                  {"records": manifest["records"], "source": manifest.get("source")}, token)
    print(f"导入清单: 新增 {result['created']}，更新 {result['updated']}，跳过 {result['skipped']}")

    # stem -> title，用于把转写挂到对应视频
    title_by_stem = {}
    for record in manifest["records"]:
        if record.get("video_out"):
            stem = Path(record["video_out"]).stem
            title_by_stem[stem] = record["title"]

    videos = call(args.base, "/api/videos?limit=500", token=token)["items"]
    id_by_title = {video["title"]: video["id"] for video in videos}

    transcript_dir = Path(args.transcript)
    pushed = 0
    for json_path in sorted(transcript_dir.glob("*.json")):
        data = json.loads(json_path.read_text(encoding="utf-8"))
        title = title_by_stem.get(json_path.stem)
        video_id = id_by_title.get(title)
        if not video_id:
            print(f"  [跳过] 找不到视频: {json_path.name}")
            continue
        result = call(args.base, f"/api/videos/{video_id}/transcript", "POST", {
            "model": data.get("model", "small.en"),
            "language": data.get("language", "en"),
            "text": data.get("text", ""),
            "segments": data.get("segments"),
            "speech_ms": int(round(data.get("speech_s", 0) * 1000)),
            "params_hash": None,
        }, token)
        metrics = result["metrics"]
        print(f"  {title}: {metrics['level']} 生词率 {metrics['oov_ratio']:.0%} "
              f"唯一词 {metrics['unique_words']} WPM {metrics['wpm']:.0f}")
        pushed += 1

    print(f"\n完成：{pushed} 集转写入库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
