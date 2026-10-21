#!/usr/bin/env python3
"""诊断：不做任何过滤，打印原始 segment 及其质量指标，用于调阈值。

用法：python tools/diag_segments.py --file "prepared/asr/xxx.wav"
"""
import argparse
import os
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent


def _mkdtemp(suffix=None, prefix=None, dir=None) -> str:
    base = dir or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    for _ in range(100):
        name = os.path.join(base, f"{prefix or 'tmp'}{uuid.uuid4().hex[:10]}{suffix or ''}")
        try:
            os.makedirs(name, exist_ok=False)
            return name
        except FileExistsError:
            continue
    raise FileExistsError


tempfile.mkdtemp = _mkdtemp  # type: ignore[assignment]

LIBS = PROJECT / ".asr-libs"
if LIBS.is_dir() and str(LIBS) not in sys.path:
    sys.path.insert(0, str(LIBS))

os.environ.setdefault("HF_HOME", str(PROJECT / ".hf-cache"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

sys.path.insert(0, str(HERE))
from transcribe import load_audio  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--model", default="small.en")
    parser.add_argument("--vad", default="on", choices=["on", "off"])
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--condition", default="off", choices=["on", "off"])
    args = parser.parse_args()

    from faster_whisper import WhisperModel

    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=3,
                         download_root=str(PROJECT / ".hf-cache"))
    segments, info = model.transcribe(
        load_audio(Path(args.file)),
        language="en",
        beam_size=5,
        vad_filter=args.vad == "on",
        vad_parameters={"threshold": args.vad_threshold, "min_silence_duration_ms": 500},
        condition_on_previous_text=args.condition == "on",
    )
    print(f"duration={info.duration:.1f}s  vad={args.vad}  thr={args.vad_threshold}  condition={args.condition}")
    print(f"{'start':>7} {'end':>7} {'logprob':>8} {'nsp':>6} {'cratio':>7}  text")
    total_words = 0
    for seg in segments:
        words = len(seg.text.split())
        total_words += words
        print(f"{seg.start:7.2f} {seg.end:7.2f} {seg.avg_logprob:8.3f} "
              f"{seg.no_speech_prob:6.3f} {seg.compression_ratio:7.2f}  {seg.text.strip()}")
    print(f"total words={total_words}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main())
