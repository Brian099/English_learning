"""入库流水线：新视频 → 轻量音轨 → 转写 → 难度分级。

设计目标是"拷进去就不用管了"：扫描到新视频后自动跑完这几步，
家长端只看到进度条，跑完就能按难度排片。

三步各自幂等，可以随时中断重跑：
  1. 抽音轨  cache/audio/<id>.m4a   （64k AAC，体积约视频 1/35）
  2. 抽 ASR 输入 cache/asr/<id>.wav  （16k 单声道）
  3. 转写    transcript 表 + 回填 video 的难度字段
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .level import analyze_text
from .media_tools import ffmpeg
from .models import Transcript, Video

# 幻觉过滤阈值：素材音乐连续铺底，no_speech_prob 不可信（详见 docs/transcription-report.md）
COMPRESSION_RATIO_MAX = 2.4
AVG_LOGPROB_MIN = -1.0
HALLUCINATION_PHRASES = (
    "thank you for watching", "thanks for watching", "please subscribe",
    "subscribe to my channel", "subtitles by", "amara.org", "www.",
)
TOKEN_RE = re.compile(r"[A-Za-z']+")

_state: dict = {
    "running": False,
    "stage": "",
    "total": 0,
    "done": 0,
    "failed": 0,
    "skipped": 0,
    "cleaned": 0,
    "current": "",
    "started_at": None,
    "finished_at": None,
    "last_error": None,
}


def state() -> dict:
    return dict(_state)


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------- 媒体处理

def _run_ffmpeg(args: list[str]) -> tuple[bool, str | None]:
    executable = ffmpeg()
    if executable is None:
        return False, "找不到 ffmpeg"
    result = subprocess.run([executable, "-y", "-hide_banner", "-loglevel", "error", *args],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return False, (result.stderr or "ffmpeg 失败")[:200]
    return True, None


def measure_loudness(src: Path) -> dict | None:
    """第一遍：EBU R128 测量，拿到增益参数给第二遍用（两遍才能精确线性归一）。"""
    executable = ffmpeg()
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "-hide_banner", "-nostats", "-i", str(src), "-vn",
         "-af", "loudnorm=print_format=json", "-f", "null", os.devnull],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    text = result.stderr or ""
    values: dict[str, str] = {}
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        matches = re.findall(rf'"{key}"\s*:\s*"(-?[\d.]+|inf|-inf)"', text)
        if matches:
            values[key] = matches[-1]
    return values or None


def loudnorm_filter(measured: dict | None) -> str | None:
    """第二遍的滤镜串；没测量到就退回单遍动态归一。

    末尾再挂一个限制器：短素材线性归一失败时 loudnorm 会掉到动态模式，
    峰值可能超过 0 dBFS 削波（实测有一集 +1.5 dBTP）。限制器兜住 -1 dBFS。
    """
    base = f"loudnorm=I={settings.target_lufs}:TP={settings.target_tp}:LRA={settings.target_lra}"
    if measured:
        base += (
            f":measured_I={measured.get('input_i', -23)}"
            f":measured_TP={measured.get('input_tp', -2)}"
            f":measured_LRA={measured.get('input_lra', 7)}"
            f":measured_thresh={measured.get('input_thresh', -34)}"
            f":offset={measured.get('target_offset', 0)}:linear=true"
        )
    return f"{base},alimiter=limit=0.707:level=disabled"


def ensure_audio(src: Path, dst: Path, af: str | None = None) -> tuple[bool, str | None]:
    """抽播放用音轨：64k AAC + faststart（索引在文件头，浏览器才能流式播）。"""
    if dst.is_file() and dst.stat().st_size > 0:
        return True, None
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(src), "-vn"]
    if af:
        args += ["-af", af]
    # 96k 而不是 64k：AAC 编码会有过冲，码率太低真峰值会顶到 0 dBTP 以上
    args += ["-c:a", "aac", "-b:a", "96k", "-ac", "2",
             "-movflags", "+faststart", str(dst)]
    return _run_ffmpeg(args)


def ensure_normalized_video(src: Path, dst: Path, af: str | None = None) -> tuple[bool, str | None]:
    """把视频流原样复制、只重编音频做音量统一，写到 cache。

    媒体目录是只读挂载的，不能原地改；播放时优先用这份 cache 版本。
    代价是额外占一份视频空间（约等于原文件大小）。
    """
    if dst.is_file() and dst.stat().st_size > 0:
        return True, None
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy"]
    if af:
        args += ["-af", af]
    args += ["-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(dst)]
    return _run_ffmpeg(args)


def ensure_wav(src: Path, dst: Path) -> tuple[bool, str | None]:
    """抽 ASR 输入：16k 单声道 PCM。"""
    if dst.is_file() and dst.stat().st_size > 0:
        return True, None
    dst.parent.mkdir(parents=True, exist_ok=True)
    return _run_ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", str(dst)])


# ---------------------------------------------------------------- 转写

_model = None


def _load_model():
    """懒加载模型；只在第一次入库时下载（约 480MB，落到 /models 卷里）。"""
    global _model
    if _model is not None:
        return _model

    os.environ.setdefault("HF_HOME", settings.model_dir)
    os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    from faster_whisper import WhisperModel

    _model = WhisperModel(
        settings.whisper_model,
        device="cpu",
        compute_type=settings.whisper_compute,
        cpu_threads=settings.whisper_threads,
        download_root=settings.model_dir,
    )
    return _model


def _load_wav(path: Path):
    """把 16k 单声道 WAV 解成 float32 数组。

    faster-whisper 默认用 av 解码音频，但 av 19 与它的 metadata_errors 参数
    不兼容（TypeError），换 av 版本又会牵连别的依赖。我们本来就已经用 ffmpeg
    转成标准 16k 单声道了，直接喂数组最稳。
    """
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as handle:
        if (handle.getnchannels() != 1 or handle.getframerate() != 16000
                or handle.getsampwidth() != 2):
            return None
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe_wav(wav_path: Path) -> dict:
    """同步转写（在线程里跑）。返回 text / segments / speech_ms / words。"""
    model = _load_model()
    audio = _load_wav(wav_path)
    if audio is None:
        raise RuntimeError("WAV 格式不符合预期（需要 16k 单声道 16bit）")

    segments_iter, info = model.transcribe(
        audio,
        language="en",
        beam_size=5,
        vad_filter=False,                      # 音乐铺底时 VAD 会把对白整段切掉
        condition_on_previous_text=False,      # 切断上下文，抑制复读幻觉
        word_timestamps=False,
    )

    kept: list[dict] = []
    for raw in segments_iter:
        text = (raw.text or "").strip()
        if not text:
            continue
        if (raw.compression_ratio or 0) > COMPRESSION_RATIO_MAX:
            continue
        if (raw.avg_logprob or 0) < AVG_LOGPROB_MIN:
            continue
        if any(phrase in text.lower() for phrase in HALLUCINATION_PHRASES):
            continue
        if kept and text == kept[-1]["text"]:      # 相邻重复去掉
            kept[-1]["end"] = round(raw.end, 2)
            continue
        kept.append({"start": round(raw.start, 2), "end": round(raw.end, 2), "text": text})

    full_text = " ".join(seg["text"] for seg in kept)
    speech_s = sum(max(0.0, seg["end"] - seg["start"]) for seg in kept)
    return {
        "text": full_text,
        "segments": kept,
        "speech_ms": int(speech_s * 1000),
        "words": len(TOKEN_RE.findall(full_text)),
        "duration_s": float(info.duration or 0.0),
    }


# ---------------------------------------------------------------- 主流程

async def _ingest_one(db, video: Video) -> str:
    """处理单个视频，返回 'ok' / 'skipped' / 'failed'。"""
    src = Path(settings.media_root) / video.rel_path
    if not src.is_file():
        _state["last_error"] = f"源文件不存在: {video.rel_path}"
        return "failed"

    audio_path = Path(settings.cache_root) / "audio" / f"{video.id}.m4a"
    wav_path = Path(settings.cache_root) / "asr" / f"{video.id}.wav"
    video_path = Path(settings.cache_root) / "video" / f"{video.id}.mp4"

    # 1) 音量测量（两遍归一的前提）
    af = None
    if settings.normalize_audio or settings.normalize_video:
        measured = await asyncio.to_thread(measure_loudness, src)
        af = loudnorm_filter(measured)
        if measured and measured.get("input_i"):
            try:
                video.lufs = float(measured["input_i"])   # 记录原始响度，便于排查
            except (TypeError, ValueError):
                pass

    # 2) 播放音轨（带音量统一）
    ok, error = await asyncio.to_thread(ensure_audio, src, audio_path, af)
    if not ok:
        _state["last_error"] = f"{video.title} 抽音轨失败: {error}"
        return "failed"

    # 2b) 量一下归一化后的实际响度，存库供播放端做静态校正
    #     （归一化不是 100% 精确，实测有 ±1.5 dB 的残差；存下来让前端一次修正到位）
    out_measured = await asyncio.to_thread(measure_loudness, audio_path)
    if out_measured and out_measured.get("input_i"):
        try:
            video.audio_lufs = float(out_measured["input_i"])
        except (TypeError, ValueError):
            pass

    # 3) 看模式也归一化（可选，会额外占一份视频空间）
    if settings.normalize_video:
        ok, error = await asyncio.to_thread(ensure_normalized_video, src, video_path, af)
        if not ok:
            _state["last_error"] = f"{video.title} 视频归一化失败: {error}"

    # 2) 已有转写就跳过（幂等）
    existing = (
        await db.execute(select(Transcript).where(Transcript.video_id == video.id))
    ).scalar_one_or_none()
    if existing and existing.text:
        _state["skipped"] += 1
        return "skipped"

    # 3) ASR 输入
    ok, error = await asyncio.to_thread(ensure_wav, src, wav_path)
    if not ok:
        _state["last_error"] = f"{video.title} 抽音频失败: {error}"
        return "failed"

    # 4) 转写（重活放线程）
    try:
        result = await asyncio.to_thread(transcribe_wav, wav_path)
    except Exception as exc:
        _state["last_error"] = f"{video.title} 转写失败: {str(exc)[:160]}"
        return "failed"

    # 5) 落库 + 分级
    metrics = analyze_text(result["text"], result["speech_ms"] / 1000)
    transcript = existing or Transcript(video_id=video.id)
    transcript.model = settings.whisper_model
    transcript.language = "en"
    transcript.text = result["text"]
    transcript.segments = result["segments"]
    transcript.speech_ms = result["speech_ms"]
    transcript.words = result["words"]
    if existing is None:
        db.add(transcript)

    video.speech_ms = result["speech_ms"]
    if video.duration_ms:
        # 分段时长累加可能略微超过总时长（片段首尾有 padding），钳到 100%
        video.speech_pct = min(100.0, round(result["speech_ms"] / video.duration_ms * 100, 1))
    video.wpm = metrics["wpm"]
    video.unique_words = metrics["unique_words"]
    video.oov_ratio = metrics["oov_ratio"]
    video.level = metrics["level"]
    video.level_score = metrics["level_score"]
    return "ok"


async def run_ingest(only_ids: list[int] | None = None) -> None:
    """后台任务入口：处理所有缺音轨或缺转写的视频。"""
    _state.update(running=True, stage="准备中", total=0, done=0, failed=0,
                  skipped=0, cleaned=0, current="", started_at=_now(),
                  finished_at=None, last_error=None)
    try:
        async with SessionLocal() as db:
            stmt = select(Video).order_by(Video.id)
            if only_ids:
                stmt = stmt.where(Video.id.in_(only_ids))
            videos = (await db.execute(stmt)).scalars().all()
            _state["total"] = len(videos)

            for video in videos:
                _state["current"] = video.title
                _state["stage"] = "统一音量 / 转音频 / 提取对白"
                try:
                    outcome = await _ingest_one(db, video)
                    await db.commit()
                except Exception as exc:
                    await db.rollback()
                    outcome = "failed"
                    _state["last_error"] = f"{video.title}: {str(exc)[:160]}"

                if outcome == "failed":
                    _state["failed"] += 1
                else:
                    _state["done"] += 1
                    # 落库成功后才删 ASR 输入（失败的那集留着，方便排查）
                    _cleanup_asr_wav(video.id)
    except Exception as exc:
        _state["last_error"] = f"任务异常: {exc}"
    finally:
        _state.update(running=False, stage="", current="", finished_at=_now())


def _cleanup_asr_wav(video_id: int) -> None:
    """删掉 ASR 输入的 wav：转写已经入库，这份 32KB/s 的中间文件没用了。

    删不掉只记 warning，不影响入库结果；需要重转写时会从视频重新抽。
    """
    if settings.keep_asr_wav:
        return
    wav = Path(settings.cache_root) / "asr" / f"{video_id}.wav"
    if not wav.is_file():
        return
    try:
        wav.unlink()
        _state["cleaned"] += 1
    except OSError as exc:
        _state["last_error"] = f"删除 {wav.name} 失败: {exc}"


async def pending_count() -> int:
    """有多少视频还没转写（用于判断要不要跑）。"""
    async with SessionLocal() as db:
        rows = (await db.execute(select(Video.id))).scalars().all()
        if not rows:
            return 0
        done = set(
            (await db.execute(
                select(Transcript.video_id).where(Transcript.text.is_not(None))
            )).scalars().all()
        )
    return len([vid for vid in rows if vid not in done])
