#!/usr/bin/env python3
"""样本预处理流水线：过滤 → 清洗命名 → 统一响度 → 抽音轨 → 生成清单

用法：
    python tools/prep.py --src "视频样本" --out prepared --exclude 闹闹知识屋
    python tools/prep.py --src ... --dry-run          # 只出清单，不处理

产出：
    prepared/video/<系列>/<系列>_S01E01_<标题>.mp4   统一到 -16 LUFS，视频流直接复制
    prepared/audio/<系列>/<系列>_S01E01_<标题>.m4a   磨耳朵用的音轨（体积约视频的 1/27）
    prepared/asr/<系列>/<系列>_S01E01_<标题>.wav     16k mono，喂给 Whisper
    prepared/manifest.csv / manifest.json
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

VIDEO_EXT = {".mp4", ".mkv", ".m4v", ".mov", ".webm", ".avi"}

# 系列名 → 是否英语内容。默认按关键字排除中文系列
DEFAULT_EXCLUDE = ["睡前故事", "知识屋", "中文", "儿歌"]

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u200D\u2b00-\u2bff]"
)
HASH_RE = re.compile(r"[_\-]{2,}[0-9a-f]{16,}$", re.IGNORECASE)


def find_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if sys.platform == "win32" else "")
    for base in (Path(r"D:\ffmpeg\bin"), Path(r"C:\ffmpeg\bin"), Path("/usr/bin"), Path("/usr/local/bin")):
        candidate = base / exe
        if candidate.exists():
            return str(candidate)
    sys.exit(f"找不到 {name}，请安装 ffmpeg 或加入 PATH")


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
NULL_SINK = "NUL" if sys.platform == "win32" else "/dev/null"


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


# ---------------------------------------------------------------- 探测

@dataclass
class Probe:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    channels: int = 0
    sample_rate: int = 0
    audio_kbps: int = 0
    subtitle_streams: int = 0


def probe(path: Path) -> Probe:
    res = run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)])
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {path}\n{res.stderr[:400]}")
    data = json.loads(res.stdout)
    info = Probe()
    info.duration = float(data.get("format", {}).get("duration") or 0.0)
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video" and not info.vcodec:
            info.vcodec = stream.get("codec_name", "")
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            rate = stream.get("avg_frame_rate") or "0/1"
            try:
                num, _, den = rate.partition("/")
                info.fps = round(float(num) / float(den or 1), 2)
            except (ValueError, ZeroDivisionError):
                info.fps = 0.0
        elif kind == "audio" and not info.acodec:
            info.acodec = stream.get("codec_name", "")
            info.channels = int(stream.get("channels") or 0)
            info.sample_rate = int(stream.get("sample_rate") or 0)
            info.audio_kbps = round(int(stream.get("bit_rate") or 0) / 1000)
        elif kind == "subtitle":
            info.subtitle_streams += 1
    return info


def measure_loudness(path: Path) -> dict:
    """EBU R128 第一遍测量。返回 input_i / input_tp / input_lra / input_thresh / target_offset。"""
    res = run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-vn",
         "-af", "loudnorm=print_format=json", "-f", "null", NULL_SINK]
    )
    text = res.stderr
    keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    out: dict[str, str] = {}
    for key in keys:
        matches = re.findall(rf'"{key}"\s*:\s*"(-?[\d.]+|inf|-inf)"', text)
        if matches:
            out[key] = matches[-1]
    return out


def silence_percent(path: Path, duration: float) -> float:
    if duration <= 0:
        return 0.0
    res = run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
         "-af", "silencedetect=n=-30dB:d=0.6", "-f", "null", NULL_SINK]
    )
    total = sum(float(v) for v in re.findall(r"silence_duration: ([\d.]+)", res.stderr))
    return round(total / duration * 100, 1)


# ---------------------------------------------------------------- 命名

def split_series(stem: str) -> tuple[str, str]:
    """从 '(系列名)标题' 里拆出系列名。"""
    match = re.match(r"^\s*[（(]([^）)]+)[）)]\s*(.*)$", stem)
    if match:
        return match.group(1).strip(), match.group(2)
    return "未分类", stem


BREAK_RE = re.compile(r"[。！？；，,、“”‘’「」『』【】（）()\[\]]")


def clean_title(rest: str, limit: int = 20) -> str:
    """把抖音文案压成一个短标题。优先取《》，其次取分隔符后的一段。"""
    text = HASH_RE.sub("", rest)
    text = re.sub(r"#\S+", " ", text)          # 话题标签
    text = EMOJI_RE.sub(" ", text)
    text = re.sub(r"[｜|]", "｜", text)

    book = re.search(r"《([^》]+)》", text)
    if book:
        title = book.group(1)
    else:
        parts = [p.strip() for p in text.split("｜") if p.strip()]
        candidate = parts[-1] if len(parts) > 1 else (parts[0] if parts else text)
        # "英语启蒙动画_小蚂蚁搬家…" 这种下划线拼接，取最后一段
        if "_" in candidate:
            candidate = candidate.rsplit("_", 1)[-1]
        candidate = BREAK_RE.split(candidate)[0].strip()
        # 文案里常跟一句说明，取第一个词块
        head = candidate.split(" ")[0].strip()
        if len(head) >= 3:
            candidate = head
        title = candidate or candidate

    title = re.sub(r"[\\/:*?\"<>|]", "", title)
    title = re.sub(r"\s+", " ", title).strip(" _-—~·.、\"'“”")
    return title[:limit] or "未命名"


def safe_name(text: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|]", "_", text)
    return re.sub(r"\s+", " ", text).strip() or "unnamed"


# ---------------------------------------------------------------- 处理

@dataclass
class Record:
    series: str
    season: int
    episode: int
    title: str
    original_name: str
    duration_sec: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    audio_kbps: int = 0
    subtitle_streams: int = 0
    lufs_before: float | None = None
    truepeak_before: float | None = None
    lufs_after: float | None = None
    truepeak_after: float | None = None
    silence_pct: float = 0.0
    size_mb: float = 0.0
    video_out: str = ""
    audio_out: str = ""
    asr_wav: str = ""
    status: str = "pending"
    note: str = ""

    @property
    def stem(self) -> str:
        return f"{safe_name(self.series)}_S{self.season:02d}E{self.episode:02d}_{safe_name(self.title)}"


def load_numbering(path: Path) -> dict[str, int]:
    """沿用上一次的集号，保证重复运行不重新编号。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {row["original_name"]: row["episode"] for row in data.get("records", [])}


def process(record: Record, src: Path, out: Path, args) -> None:
    info = probe(src)
    record.duration_sec = round(info.duration, 1)
    record.width, record.height, record.fps = info.width, info.height, info.fps
    record.video_codec, record.audio_codec = info.vcodec, info.acodec
    record.audio_kbps = info.audio_kbps
    record.subtitle_streams = info.subtitle_streams
    record.size_mb = round(src.stat().st_size / 1e6, 1)

    before = measure_loudness(src)
    record.lufs_before = float(before["input_i"]) if "input_i" in before else None
    record.truepeak_before = float(before["input_tp"]) if "input_tp" in before else None
    record.silence_pct = silence_percent(src, info.duration)

    video_dir = out / "video" / safe_name(record.series)
    audio_dir = out / "audio" / safe_name(record.series)
    asr_dir = out / "asr" / safe_name(record.series)
    for directory in (video_dir, audio_dir, asr_dir):
        directory.mkdir(parents=True, exist_ok=True)

    video_out = video_dir / f"{record.stem}.mp4"
    audio_out = audio_dir / f"{record.stem}.m4a"
    wav_out = asr_dir / f"{record.stem}.wav"

    if args.dry_run:
        record.status = "dry-run"
        return

    # 1) 统一响度：视频流直接复制，只重编音频
    af = (
        f"loudnorm=I={args.target_lufs}:TP={args.target_tp}:LRA={args.target_lra}"
        f":measured_I={before.get('input_i', '-23')}"
        f":measured_TP={before.get('input_tp', '-2')}"
        f":measured_LRA={before.get('input_lra', '7')}"
        f":measured_thresh={before.get('input_thresh', '-34')}"
        f":offset={before.get('target_offset', '0')}"
        ":linear=true"
    )
    res = run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
        "-af", af, "-c:a", "aac", "-b:a", "96k", "-ar", "44100",
        "-movflags", "+faststart", str(video_out),
    ])
    if res.returncode != 0:
        record.status = "failed"
        record.note = f"响度处理失败: {res.stderr[:200]}"
        return

    after = measure_loudness(video_out)
    record.lufs_after = float(after["input_i"]) if "input_i" in after else None
    record.truepeak_after = float(after["input_tp"]) if "input_tp" in after else None

    # 2) 磨耳朵音轨（体积约视频的 1/27）
    #    +faststart 把 moov 索引放到文件头，否则浏览器要先去文件尾取索引，
    #    流式播放（Range）容易直接报 MEDIA_ERR_SRC_NOT_SUPPORTED
    res = run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_out),
        "-vn", "-c:a", "aac", "-b:a", "64k", "-ac", "2",
        "-movflags", "+faststart", str(audio_out),
    ])
    if res.returncode != 0:
        record.note = "音轨提取失败"

    # 3) ASR 输入：16k 单声道 wav
    res = run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_out),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_out),
    ])
    if res.returncode != 0:
        record.note = (record.note + " ASR wav 失败").strip()

    record.video_out = str(video_out.relative_to(out))
    record.audio_out = str(audio_out.relative_to(out))
    record.asr_wav = str(wav_out.relative_to(out))
    record.status = "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="视频样本预处理流水线")
    parser.add_argument("--src", required=True, help="样本目录")
    parser.add_argument("--out", default="prepared", help="输出目录")
    parser.add_argument("--exclude", nargs="*", default=DEFAULT_EXCLUDE, help="要排除的系列关键字")
    parser.add_argument("--target-lufs", type=float, default=-16.0)
    parser.add_argument("--target-tp", type=float, default=-1.5)
    parser.add_argument("--target-lra", type=float, default=11.0)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只出清单，不转码")
    args = parser.parse_args()

    src_root = Path(args.src)
    out_root = Path(args.out)
    if not src_root.is_dir():
        print(f"源目录不存在: {src_root}")
        return 2
    out_root.mkdir(parents=True, exist_ok=True)

    files = sorted(
        (p for p in src_root.rglob("*") if p.suffix.lower() in VIDEO_EXT and p.is_file()),
        key=lambda p: p.name,
    )

    numbering = load_numbering(out_root / "manifest.json")
    counters: dict[str, int] = {}
    records: list[Record] = []
    skipped: list[tuple[str, str]] = []

    for path in files:
        series, rest = split_series(path.stem)
        if any(word in series for word in args.exclude):
            skipped.append((path.name, f"系列命中排除词: {series}"))
            continue
        counters[series] = counters.get(series, 0) + 1
        episode = numbering.get(path.name, counters[series])
        records.append(
            Record(
                series=series,
                season=1,
                episode=episode,
                title=clean_title(rest),
                original_name=path.name,
                note=str(path.parent.relative_to(src_root)) if path.parent != src_root else "",
            )
        )

    if args.limit:
        records = records[: args.limit]

    print(f"发现 {len(files)} 个视频，排除 {len(skipped)} 个，待处理 {len(records)} 个")
    for name, reason in skipped:
        print(f"  [跳过] {name[:40]}…  ({reason})")

    for index, record in enumerate(records, 1):
        src = next(p for p in files if p.name == record.original_name)
        print(f"[{index}/{len(records)}] {record.series} S{record.season:02d}E{record.episode:02d} {record.title}")
        try:
            process(record, src, out_root, args)
        except Exception as exc:  # 单个失败不中断整批
            record.status = "failed"
            record.note = str(exc)[:200]
        if record.status == "ok":
            print(
                f"    {record.duration_sec}s  {record.width}x{record.height}  "
                f"LUFS {record.lufs_before} → {record.lufs_after}  "
                f"TP {record.truepeak_before} → {record.truepeak_after}  "
                f"静音 {record.silence_pct}%"
            )
        elif record.status == "failed":
            print(f"    失败: {record.note}")

    # 清单
    payload = {
        "source": str(src_root),
        "target_lufs": args.target_lufs,
        "skipped": [{"file": name, "reason": reason} for name, reason in skipped],
        "records": [asdict(r) for r in records],
    }
    (out_root / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    columns = list(asdict(records[0]).keys()) if records else []
    with (out_root / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))

    ok = sum(1 for r in records if r.status == "ok")
    failed = [r for r in records if r.status == "failed"]
    print(f"\n完成 {ok}/{len(records)}，失败 {len(failed)}")
    for record in failed:
        print(f"  ✗ {record.original_name[:50]} — {record.note}")
    print(f"清单: {out_root / 'manifest.csv'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main())
