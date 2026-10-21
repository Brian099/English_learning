#!/usr/bin/env python3
"""用 faster-whisper 把音频转成带时间戳的文本，并过滤幻觉。

用法：
    python tools/transcribe.py --src prepared/asr --out prepared/transcript
    python tools/transcribe.py --src prepared/asr --out prepared/transcript --limit 1
    python tools/transcribe.py --src prepared/asr --out prepared/transcript --model medium.en

产出：
    <out>/<名称>.json    完整 segments（start/end/text/logprob）
    <out>/<名称>.srt     字幕
    <out>/transcript.csv 汇总：语音时长、占比、词数、语速
    <out>/all.txt        所有文本拼在一起，方便通读校对
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent


def _sandbox_safe_mkdtemp(suffix=None, prefix=None, dir=None) -> str:
    """沙箱里 tempfile.mkdtemp 建出的目录不可读写，换成 os.makedirs 实现。"""
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


tempfile.mkdtemp = _sandbox_safe_mkdtemp  # type: ignore[assignment]

# 依赖装在项目内，避免污染全局环境
LIBS = PROJECT / ".asr-libs"
if LIBS.is_dir() and str(LIBS) not in sys.path:
    sys.path.insert(0, str(LIBS))

# 模型缓存放工作区内（沙箱只允许写工作区）
os.environ.setdefault("HF_HOME", str(PROJECT / ".hf-cache"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
# HuggingFace 直连很慢，默认走镜像
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

AUDIO_EXT = {".wav", ".m4a", ".mp3", ".flac", ".mp4"}

# 幻觉过滤阈值
# 注意：本批素材音乐/音效连续铺底，no_speech_prob 严重失真——
# 实测正常对白段 nsp 可达 0.92，所以这个阈值必须放到接近 1，几乎不生效。
NO_SPEECH_PROB_MAX = 0.98
COMPRESSION_RATIO_MAX = 2.4   # 复读机式输出压缩比异常
AVG_LOGPROB_MIN = -1.0        # 置信度太低的段

# 训练数据里常见的字幕组用语，纯音乐段容易凭空生成
HALLUCINATION_PHRASES = (
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "subscribe to my channel",
    "subtitles by",
    "amara.org",
    "www.",
    "see you next time",
)


@dataclass
class Segment:
    start: float
    end: float
    text: str
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    compression_ratio: float = 0.0


@dataclass
class Result:
    source: str
    model: str
    language: str
    duration_s: float = 0.0
    speech_s: float = 0.0
    speech_pct: float = 0.0
    segments_total: int = 0
    segments_kept: int = 0
    chars: int = 0
    words: int = 0
    wpm: float = 0.0
    elapsed_s: float = 0.0
    status: str = "ok"
    note: str = ""


def fmt_time(seconds: float, sep: str = ",") -> str:
    ms = int(round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{ms:03d}"


def dedupe(segments: list[Segment]) -> list[Segment]:
    """去掉相邻的重复文本（复读机幻觉的典型表现）。"""
    out: list[Segment] = []
    for seg in segments:
        if out and seg.text.strip() and seg.text.strip() == out[-1].text.strip():
            out[-1].end = seg.end
            continue
        out.append(seg)
    return out


def load_audio(path: Path):
    """把 16k 单声道 WAV 解成 float32 numpy 数组。

    faster-whisper 默认用 av 解码，但本机 av 19 与它的 metadata_errors
    参数不兼容，直接喂数组可以完全绕开 av。
    """
    if path.suffix.lower() != ".wav":
        return str(path)
    try:
        import wave

        import numpy as np

        with wave.open(str(path), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getframerate() != 16000 or handle.getsampwidth() != 2:
                return str(path)
            raw = handle.readframes(handle.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        return str(path)


def transcribe_one(model, path: Path, out_dir: Path, language: str, model_name: str,
                   vad_threshold: float = 0.25) -> Result:
    result = Result(source=path.name, model=model_name, language=language)
    started = time.time()

    segments_iter, info = model.transcribe(
        load_audio(path),
        language=language,
        beam_size=5,
        # VAD 阈值必须调低：默认 0.5 会把音乐铺底下的对白整段切掉
        # （实测同一集：0.5 → 72 词，0.25 → 182 词，关闭 → 192 词）
        vad_filter=vad_threshold > 0,
        vad_parameters={"threshold": vad_threshold, "min_silence_duration_ms": 500},
        condition_on_previous_text=False,   # 关键：切断上下文，显著降低复读幻觉
        word_timestamps=False,
    )

    result.duration_s = round(info.duration or 0.0, 2)

    kept: list[Segment] = []
    total = 0
    for raw in segments_iter:
        total += 1
        text = (raw.text or "").strip()
        if not text:
            continue
        seg = Segment(
            start=round(raw.start, 2),
            end=round(raw.end, 2),
            text=text,
            avg_logprob=round(raw.avg_logprob or 0.0, 3),
            no_speech_prob=round(raw.no_speech_prob or 0.0, 3),
            compression_ratio=round(raw.compression_ratio or 0.0, 2),
        )
        if seg.no_speech_prob > NO_SPEECH_PROB_MAX:
            continue
        if seg.compression_ratio > COMPRESSION_RATIO_MAX:
            continue
        if seg.avg_logprob < AVG_LOGPROB_MIN:
            continue
        if any(phrase in seg.text.lower() for phrase in HALLUCINATION_PHRASES):
            continue
        kept.append(seg)

    kept = dedupe(kept)
    result.segments_total = total
    result.segments_kept = len(kept)

    speech = sum(max(0.0, seg.end - seg.start) for seg in kept)
    result.speech_s = round(speech, 2)
    result.speech_pct = round(speech / result.duration_s * 100, 1) if result.duration_s else 0.0

    text_all = " ".join(seg.text for seg in kept)
    result.chars = len(text_all)
    result.words = len(text_all.split())
    result.wpm = round(result.words / (speech / 60), 1) if speech > 0 else 0.0

    payload = {
        "source": path.name,
        "model": model_name,
        "language": language,
        "duration_s": result.duration_s,
        "speech_s": result.speech_s,
        "speech_pct": result.speech_pct,
        "segments_total": total,
        "segments_kept": len(kept),
        "text": text_all,
        "segments": [asdict(seg) for seg in kept],
    }
    (out_dir / f"{path.stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = []
    for index, seg in enumerate(kept, 1):
        lines.append(f"{index}\n{fmt_time(seg.start)} --> {fmt_time(seg.end)}\n{seg.text}\n")
    (out_dir / f"{path.stem}.srt").write_text("\n".join(lines), encoding="utf-8")

    result.elapsed_s = round(time.time() - started, 1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="faster-whisper 批量转写")
    parser.add_argument("--src", default="prepared/asr")
    parser.add_argument("--out", default="prepared/transcript")
    parser.add_argument("--model", default="small.en")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--threads", type=int, default=3, help="CPU 线程数，N100 建议 3")
    parser.add_argument("--language", default="en")
    parser.add_argument("--vad-threshold", type=float, default=0.0,
                        help="VAD 灵敏度，0 = 关闭（默认）。本批素材音乐连续铺底，"
                             "VAD 会把对白整段切掉：实测同一集 0.5→29 词、0.25→29 词、关闭→78 词")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    src = Path(args.src)
    out = Path(args.out)
    if not src.is_dir():
        print(f"源目录不存在: {src}")
        return 2
    out.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in src.rglob("*") if p.suffix.lower() in AUDIO_EXT)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("没有找到音频文件")
        return 2

    from faster_whisper import WhisperModel

    print(f"加载模型 {args.model} ({args.compute_type}, {args.threads} 线程)…")
    load_started = time.time()
    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        cpu_threads=args.threads,
        download_root=str(PROJECT / ".hf-cache"),
    )
    print(f"模型就绪，用时 {time.time() - load_started:.1f}s\n")

    results: list[Result] = []
    for index, path in enumerate(files, 1):
        print(f"[{index}/{len(files)}] {path.name}")
        try:
            result = transcribe_one(model, path, out, args.language, args.model, args.vad_threshold)
        except Exception as exc:
            results.append(
                Result(
                    source=path.name,
                    model=args.model,
                    language=args.language,
                    status="failed",
                    note=str(exc)[:200],
                )
            )
            print(f"    失败: {exc}")
            continue
        results.append(result)
        print(
            f"    音频 {result.duration_s:.1f}s → 语音 {result.speech_s:.1f}s "
            f"({result.speech_pct}%)  {result.words} 词  {result.wpm} WPM  "
            f"段 {result.segments_kept}/{result.segments_total}  耗时 {result.elapsed_s}s"
        )

    with (out / "transcript.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))

    blocks = []
    for path in files:
        json_path = out / f"{path.stem}.json"
        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            blocks.append(f"=== {path.stem} ===\n{data['text']}\n")
    (out / "all.txt").write_text("\n".join(blocks), encoding="utf-8")

    ok = sum(1 for r in results if r.status == "ok")
    total_audio = sum(r.duration_s for r in results)
    total_speech = sum(r.speech_s for r in results)
    total_words = sum(r.words for r in results)
    print(f"\n完成 {ok}/{len(results)}")
    print(f"音频 {total_audio / 60:.1f} 分钟，语音 {total_speech / 60:.1f} 分钟，共 {total_words} 词")
    print(f"输出: {out}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main())
