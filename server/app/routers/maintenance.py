"""维护任务接口：入库流水线（转音频 + 提取对白）。

真正的活儿在 app/pipeline.py，这里只负责触发和查进度。
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import pipeline
from ..config import settings
from ..db import get_db
from ..media_tools import ffmpeg
from ..models import Transcript, Video
from ..security import require_owner

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


@router.post("/ingest", dependencies=[Depends(require_owner)])
async def start_ingest(
    background: BackgroundTasks,
    only_pending: bool = True,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """给视频补音轨 + 提取对白（后台执行，用 /status 查进度）。

    only_pending=True 时跳过已经有转写的视频，重复调用是安全的。
    """
    current = pipeline.state()
    if current["running"]:
        return {"started": False, "reason": "已有任务在跑", **current}

    if ffmpeg() is None:
        return {"started": False, "reason": "容器里没有 ffmpeg，无法转音频"}

    videos = (await db.execute(select(Video.id).order_by(Video.id))).scalars().all()
    if not videos:
        return {"started": False, "reason": "库里还没有视频，先扫描目录"}

    ids = list(videos)
    if only_pending:
        done = set(
            (await db.execute(
                select(Transcript.video_id).where(Transcript.text.is_not(None))
            )).scalars().all()
        )
        ids = [vid for vid in ids if vid not in done]

    if not ids:
        return {"started": False, "reason": "所有视频都已处理过"}

    background.add_task(pipeline.run_ingest, ids)
    return {"started": True, "pending": len(ids), "model": settings.whisper_model}


# 兼容旧调用：只想要音轨的场景也走同一条流水线
@router.post("/extract-audio", dependencies=[Depends(require_owner)])
async def extract_audio_alias(
    background: BackgroundTasks, db: AsyncSession = Depends(get_db)
) -> dict:
    return await start_ingest(background, only_pending=False, db=db)


@router.get("/status", dependencies=[Depends(require_owner)])
async def maintenance_status() -> dict:
    return pipeline.state()


@router.get("/pending", dependencies=[Depends(require_owner)])
async def pending() -> dict:
    return {"pending": await pipeline.pending_count(), "model": settings.whisper_model}
