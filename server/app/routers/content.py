from pathlib import Path
from urllib.parse import quote

import mimetypes
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Series, Student, Video
from ..scan import scan_media
from ..security import read_auth, require_parent

router = APIRouter(prefix="/api", tags=["content"])


def _has_audio(video: Video) -> bool:
    """有没有独立音轨：预处理生成的（audio_rel）或服务端提取的（cache）。"""
    if video.audio_rel:
        return True
    return (Path(settings.cache_root) / "audio" / f"{video.id}.m4a").is_file()


class StudentBody(BaseModel):
    name: str
    daily_target_min: int = 20


class VideoPatch(BaseModel):
    title: str | None = None
    series_id: int | None = None
    level: str | None = None
    tags: str | None = None
    language: str | None = None


# ------------------------------------------------------------------ students

@router.get("/students", dependencies=[Depends(require_parent)])
async def list_students(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Student).order_by(Student.id))).scalars().all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "daily_target_min": s.daily_target_min,
            "active": s.active,
        }
        for s in rows
    ]


@router.post("/students", dependencies=[Depends(require_parent)])
async def create_student(body: StudentBody, db: AsyncSession = Depends(get_db)) -> dict:
    student = Student(name=body.name, daily_target_min=body.daily_target_min)
    db.add(student)
    await db.commit()
    await db.refresh(student)
    return {"id": student.id, "name": student.name}


@router.patch("/students/{student_id}", dependencies=[Depends(require_parent)])
async def patch_student(
    student_id: int, body: StudentBody, db: AsyncSession = Depends(get_db)
) -> dict:
    student = await db.get(Student, student_id)
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "学生不存在")
    student.name = body.name
    student.daily_target_min = body.daily_target_min
    await db.commit()
    return {"id": student.id, "name": student.name, "daily_target_min": student.daily_target_min}


# -------------------------------------------------------------------- series

@router.get("/series")
async def list_series(
    _auth: dict = Depends(read_auth), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    rows = (await db.execute(select(Series).order_by(Series.sort_order, Series.name))).scalars().all()
    counts = dict(
        (
            await db.execute(
                select(Video.series_id, func.count(Video.id)).group_by(Video.series_id)
            )
        ).all()
    )
    return [
        {
            "id": s.id,
            "name": s.name,
            "level": s.level,
            "language": s.language,
            "description": s.description,
            "video_count": counts.get(s.id, 0),
        }
        for s in rows
    ]


# -------------------------------------------------------------------- videos

@router.get("/videos")
async def list_videos(
    series_id: int | None = None,
    level: str | None = None,
    q: str | None = None,
    min_minutes: float | None = None,
    max_minutes: float | None = None,
    limit: int = Query(200, le=500),
    offset: int = 0,
    _auth: dict = Depends(read_auth),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(Video)
    if series_id is not None:
        stmt = stmt.where(Video.series_id == series_id)
    if level:
        stmt = stmt.where(Video.level == level)
    if q:
        stmt = stmt.where(Video.title.ilike(f"%{q}%"))
    if min_minutes is not None:
        stmt = stmt.where(Video.duration_ms >= int(min_minutes * 60_000))
    if max_minutes is not None:
        stmt = stmt.where(Video.duration_ms <= int(max_minutes * 60_000))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(stmt.order_by(Video.series_id, Video.id).limit(limit).offset(offset))
    ).scalars().all()

    return {
        "total": total,
        "items": [
            {
                "id": v.id,
                "title": v.title,
                "series_id": v.series_id,
                "duration_ms": v.duration_ms,
                "speech_pct": v.speech_pct,
                "wpm": v.wpm,
                "level": v.level,
                "level_score": v.level_score,
                "unique_words": v.unique_words,
                "oov_ratio": v.oov_ratio,
                "has_audio": _has_audio(v),
                "media_url": f"/api/media/{v.id}",
                "audio_url": f"/api/media/{v.id}?track=audio",
            }
            for v in rows
        ],
    }


@router.patch("/videos/{video_id}", dependencies=[Depends(require_parent)])
async def patch_video(
    video_id: int, body: VideoPatch, db: AsyncSession = Depends(get_db)
) -> dict:
    video = await db.get(Video, video_id)
    if video is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "视频不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(video, field, value)
    await db.commit()
    return {"id": video.id, "title": video.title, "level": video.level}


# --------------------------------------------------------------------- scan

@router.post("/scan", dependencies=[Depends(require_parent)])
async def scan(db: AsyncSession = Depends(get_db)) -> dict:
    """扫描 MEDIA_ROOT 增量入库（用于没有预处理清单的场景）。"""
    return await scan_media(db)


# -------------------------------------------------------------------- media

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
CHUNK = 64 * 1024


def _iter_file(path: Path, start: int, length: int):
    """按块读文件，避免一次性把几百 MB 读进内存。"""
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _direct_media(path: Path, request: Request) -> Response:
    """没有 nginx 时由 FastAPI 自己发文件，支持 Range（拖动进度条、断点续传都靠它）。"""
    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    range_header = request.headers.get("range")

    if range_header:
        match = RANGE_RE.match(range_header.strip())
        if match:
            start_text, end_text = match.groups()
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else size - 1
            else:  # bytes=-500 表示最后 500 字节
                length = int(end_text or 0)
                start = max(0, size - length)
                end = size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                return Response(
                    status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                    headers={"Content-Range": f"bytes */{size}"},
                )
            length = end - start + 1
            return StreamingResponse(
                _iter_file(path, start, length),
                status_code=status.HTTP_206_PARTIAL_CONTENT,
                media_type=media_type,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{size}",
                    "Content-Length": str(length),
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "private, max-age=3600",
                },
            )

    return StreamingResponse(
        _iter_file(path, 0, size),
        status_code=status.HTTP_200_OK,
        media_type=media_type,
        headers={
            "Content-Length": str(size),
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.get("/media/{video_id}")
async def media(
    video_id: int,
    request: Request,
    track: str = Query("video", pattern="^(video|audio)$"),
    _auth: dict = Depends(read_auth),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """鉴权后发文件。

    两种模式（config.serve_media）：
      - accel  ：只回 X-Accel-Redirect，由 nginx 直出，API 不搬运字节（生产）
      - direct ：FastAPI 自己按 Range 流式返回（本地开发，没有 nginx 也能播）

    track=audio 时返回仅音轨版本（体积约为视频的 1/36，磨耳朵模式用）。
    """
    video = await db.get(Video, video_id)
    if video is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "视频不存在")

    # 音轨查找顺序：预处理生成的（media 目录） → 服务端自动提取的（cache 目录）
    # → 都没有就退回视频文件本身（浏览器能解出音轨，只是体积大 36 倍）
    cache_audio = Path(settings.cache_root) / "audio" / f"{video.id}.m4a"
    rel = video.audio_rel if track == "audio" and video.audio_rel else None

    if track == "audio" and not rel and cache_audio.is_file():
        # cache 里的音轨不在 nginx 的 /_media/ 映射下，直接由 API 按 Range 发
        return _direct_media(cache_audio, request)

    rel = rel or video.rel_path
    if not rel:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该视频没有可用音轨")

    absolute = Path(settings.media_root) / rel
    if not absolute.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不在磁盘上，请检查挂载")

    if settings.serve_media == "direct":
        return _direct_media(absolute, request)

    internal = "/_media/" + quote(rel, safe="/")
    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            "X-Accel-Redirect": internal,
            "Cache-Control": "private, max-age=3600",
        },
    )
