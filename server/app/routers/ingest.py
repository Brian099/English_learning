from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..level import analyze_text
from ..models import Series, Transcript, Video
from ..security import current_device, read_auth, require_owner

router = APIRouter(prefix="/api", tags=["ingest"])


# ------------------------------------------------------------------ manifest

class ManifestRecord(BaseModel):
    """与 tools/prep.py 产出的 manifest.json 一一对应。"""

    series: str
    season: int = 1
    episode: int = 1
    title: str
    original_name: str | None = None
    duration_sec: float = 0.0
    width: int = 0
    height: int = 0
    lufs_after: float | None = None
    size_mb: float = 0.0
    video_out: str = ""
    audio_out: str | None = None
    status: str = "ok"


class ManifestBody(BaseModel):
    records: list[ManifestRecord] = Field(default_factory=list)
    source: str | None = None


@router.post("/import/manifest", dependencies=[Depends(require_owner)])
async def import_manifest(body: ManifestBody, db: AsyncSession = Depends(get_db)) -> dict:
    """把预处理清单灌进库：按 video_out 路径做 upsert，重复导入幂等。"""
    created = updated = skipped = 0

    for record in body.records:
        if record.status not in ("ok", "dry-run"):
            skipped += 1
            continue
        rel_path = (record.video_out or "").replace("\\", "/")
        if not rel_path:
            skipped += 1
            continue

        series = (
            await db.execute(select(Series).where(Series.name == record.series))
        ).scalar_one_or_none()
        if series is None:
            series = Series(name=record.series)
            db.add(series)
            await db.flush()

        video = (
            await db.execute(select(Video).where(Video.rel_path == rel_path))
        ).scalar_one_or_none()
        values = {
            "title": record.title,
            "original_name": record.original_name,
            "series_id": series.id,
            "duration_ms": int(record.duration_sec * 1000),
            "width": record.width,
            "height": record.height,
            "lufs": record.lufs_after,
            "size_bytes": int(record.size_mb * 1_000_000),
            "audio_rel": (record.audio_out or "").replace("\\", "/") or None,
        }
        if video is None:
            video = Video(rel_path=rel_path, **values)
            db.add(video)
            created += 1
        else:
            for key, value in values.items():
                setattr(video, key, value)
            updated += 1

    await db.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


# ---------------------------------------------------------------- transcript

class TranscriptBody(BaseModel):
    model: str = "small.en"
    language: str = "en"
    text: str = ""
    segments: list[dict] | None = None
    speech_ms: int = 0
    params_hash: str | None = None


@router.post("/videos/{video_id}/transcript", dependencies=[Depends(require_owner)])
async def upsert_transcript(
    video_id: int, body: TranscriptBody, db: AsyncSession = Depends(get_db)
) -> dict:
    """写入转写结果，同时重算难度分级。"""
    video = await db.get(Video, video_id)
    if video is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "视频不存在")

    speech_s = body.speech_ms / 1000 if body.speech_ms else 0.0
    metrics = analyze_text(body.text, speech_s)

    transcript = (
        await db.execute(select(Transcript).where(Transcript.video_id == video_id))
    ).scalar_one_or_none()
    if transcript is None:
        transcript = Transcript(video_id=video_id)
        db.add(transcript)

    transcript.model = body.model
    transcript.language = body.language
    transcript.text = body.text
    transcript.segments = body.segments
    transcript.speech_ms = body.speech_ms or int(speech_s * 1000)
    transcript.words = metrics["words"]
    transcript.params_hash = body.params_hash

    video.speech_ms = transcript.speech_ms
    if video.duration_ms:
        video.speech_pct = round(transcript.speech_ms / video.duration_ms * 100, 1)
    video.wpm = metrics["wpm"]
    video.unique_words = metrics["unique_words"]
    video.oov_ratio = metrics["oov_ratio"]
    video.level = metrics["level"]
    video.level_score = metrics["level_score"]

    await db.commit()
    return {"video_id": video_id, "metrics": metrics}


@router.get("/videos/{video_id}/transcript")
async def get_transcript(
    video_id: int, _auth: dict = Depends(read_auth), db: AsyncSession = Depends(get_db)
) -> dict:
    transcript = (
        await db.execute(select(Transcript).where(Transcript.video_id == video_id))
    ).scalar_one_or_none()
    if transcript is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该视频还没有转写")
    return {
        "video_id": video_id,
        "model": transcript.model,
        "language": transcript.language,
        "text": transcript.text,
        "segments": transcript.segments,
        "words": transcript.words,
        "speech_ms": transcript.speech_ms,
        "updated_at": transcript.updated_at.isoformat() if transcript.updated_at else None,
    }


# ------------------------------------------------------------------- detail

@router.get("/videos/{video_id}")
async def video_detail(
    video_id: int, _auth: dict = Depends(read_auth), db: AsyncSession = Depends(get_db)
) -> dict:
    video = await db.get(Video, video_id)
    if video is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "视频不存在")
    series = await db.get(Series, video.series_id) if video.series_id else None
    return {
        "id": video.id,
        "title": video.title,
        "series": series.name if series else None,
        "duration_ms": video.duration_ms,
        "speech_ms": video.speech_ms,
        "speech_pct": video.speech_pct,
        "wpm": video.wpm,
        "level": video.level,
        "level_score": video.level_score,
        "unique_words": video.unique_words,
        "oov_ratio": video.oov_ratio,
        "lufs": video.lufs,
        "width": video.width,
        "height": video.height,
        "media_url": f"/api/media/{video.id}",
        "audio_url": f"/api/media/{video.id}?track=audio",
        "updated_at": video.updated_at.isoformat() if video.updated_at else None,
    }
