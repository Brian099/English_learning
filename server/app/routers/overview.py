"""主账号概览：资源、家庭、账号、缓存、入库状态。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Account, Family, Series, Student, Transcript, Video
from ..pipeline import state as pipeline_state
from ..pipeline import pending_count
from ..security import require_owner

router = APIRouter(prefix="/api", tags=["overview"])


def _dir_size(path: Path) -> int:
    """目录里文件的总字节数（只数一层，缓存目录结构很浅）。"""
    total = 0
    try:
        for entry in path.iterdir():
            if entry.is_file():
                total += entry.stat().st_size
            elif entry.is_dir():
                for sub in entry.iterdir():
                    if sub.is_file():
                        total += sub.stat().st_size
    except OSError:
        pass
    return total


@router.get("/overview", dependencies=[Depends(require_owner)])
async def overview(db: AsyncSession = Depends(get_db)) -> dict:
    families = (await db.execute(select(func.count(Family.id)))).scalar_one()
    accounts = (await db.execute(select(func.count(Account.id)))).scalar_one()
    owners = (
        await db.execute(select(func.count(Account.id)).where(Account.role == "owner"))
    ).scalar_one()
    students = (await db.execute(select(func.count(Student.id)))).scalar_one()
    series = (await db.execute(select(func.count(Series.id)))).scalar_one()

    video_row = (
        await db.execute(
            select(
                func.count(Video.id),
                func.coalesce(func.sum(Video.duration_ms), 0),
                func.coalesce(func.sum(Video.size_bytes), 0),
            )
        )
    ).one()
    transcripts = (await db.execute(select(func.count(Transcript.id)))).scalar_one()
    graded = (
        await db.execute(select(func.count(Video.id)).where(Video.level.is_not(None)))
    ).scalar_one()
    with_audio = (
        await db.execute(select(func.count(Video.id)).where(Video.audio_rel.is_not(None)))
    ).scalar_one()

    cache_root = Path(settings.cache_root)
    # 听模式实际能播的集数 = 预处理音轨 + 服务端从视频抽出来的
    cache_audio_count = len(list((cache_root / "audio").glob("*.m4a"))) if (cache_root / "audio").is_dir() else 0
    with_audio = max(with_audio, cache_audio_count)
    cache = {
        name: _dir_size(cache_root / name)
        for name in ("audio", "asr", "video", "transcript")
    }

    state = pipeline_state()
    return {
        "family": {
            "count": families,
            "accounts": accounts,
            "owners": owners,
            "members": accounts - owners,
            "students": students,
        },
        "content": {
            "videos": video_row[0],
            "series": series,
            "total_minutes": round(video_row[1] / 60000, 1),
            "size_bytes": video_row[2],
            "transcripts": transcripts,
            "graded": graded,
            "with_audio": with_audio,
            "pending": video_row[0] - transcripts,
        },
        "cache": {
            "audio_bytes": cache["audio"],
            "asr_bytes": cache["asr"],
            "video_bytes": cache["video"],
            "transcript_bytes": cache["transcript"],
            "total_bytes": sum(cache.values()),
        },
        "ingest": {
            "running": state["running"],
            "stage": state["stage"],
            "done": state["done"],
            "total": state["total"],
            "failed": state["failed"],
            "cleaned": state.get("cleaned", 0),
            "last_error": state["last_error"],
            "pending": await pending_count(),
            "model": settings.whisper_model,
        },
        "paths": {
            "media_root": settings.media_root,
            "cache_root": settings.cache_root,
        },
    }
