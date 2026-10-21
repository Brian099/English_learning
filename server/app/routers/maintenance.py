"""维护任务：给"直接复制进来的视频"补音轨。

场景：用户不跑 tools/prep.py，直接把 mp4 拷到 MEDIA_ROOT 再扫描。
这样库里只有视频、没有音轨，听模式会退回播视频（体积大 36 倍）。

媒体目录是只读挂载的，所以提取出来的音轨写到可写的 CACHE_ROOT，
播放时由 media 接口直接按 Range 返回（见 routers/content.py）。
"""
from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal, get_db
from ..media_tools import ffmpeg
from ..models import Video
from ..security import require_parent

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])

_state: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "failed": 0,
    "skipped": 0,
    "current": "",
    "started_at": None,
    "finished_at": None,
    "last_error": None,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _extract_one(src: Path, dst: Path) -> tuple[bool, str | None]:
    """抽一条 64k AAC 音轨，带 faststart（索引放文件头，浏览器才能流式播）。"""
    executable = ffmpeg()
    if executable is None:
        return False, "找不到 ffmpeg"
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        executable, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src), "-vn", "-c:a", "aac", "-b:a", "64k", "-ac", "2",
        "-movflags", "+faststart", str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, (result.stderr or "ffmpeg 失败")[:200]
    return True, None


async def _run_extraction() -> None:
    global _state
    _state.update(running=True, done=0, failed=0, skipped=0, current="",
                  started_at=_now(), finished_at=None, last_error=None)

    try:
        async with SessionLocal() as db:
            rows = (
                await db.execute(select(Video).where(Video.audio_rel.is_(None)))
            ).scalars().all()
            _state["total"] = len(rows)

            for video in rows:
                src = Path(settings.media_root) / video.rel_path
                dst = Path(settings.cache_root) / "audio" / f"{video.id}.m4a"
                _state["current"] = video.title

                if not src.is_file():
                    _state["failed"] += 1
                    _state["last_error"] = f"源文件不存在: {video.rel_path}"
                    continue
                if dst.is_file() and dst.stat().st_size > 0:
                    _state["skipped"] += 1
                    _state["done"] += 1
                    continue

                ok, error = await asyncio.to_thread(_extract_one, src, dst)
                if ok:
                    _state["done"] += 1
                else:
                    _state["failed"] += 1
                    _state["last_error"] = f"{video.title}: {error}"
    except Exception as exc:  # 整体失败也要把状态收干净
        _state["last_error"] = f"任务异常: {exc}"
    finally:
        _state.update(running=False, current="", finished_at=_now())


@router.post("/extract-audio", dependencies=[Depends(require_parent)])
async def extract_audio(
    background: BackgroundTasks, db: AsyncSession = Depends(get_db)
) -> dict:
    """给所有没有音轨的视频补一条音轨（后台执行，用 /status 查进度）。"""
    if _state["running"]:
        return {"started": False, "reason": "已有任务在跑", **_state}

    if ffmpeg() is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "容器里没有 ffmpeg，无法提取音轨")

    pending = (
        await db.execute(select(Video.id).where(Video.audio_rel.is_(None)))
    ).scalars().all()
    if not pending:
        return {"started": False, "reason": "所有视频都已有音轨", "pending": 0}

    background.add_task(_run_extraction)
    return {"started": True, "pending": len(pending)}


@router.get("/status", dependencies=[Depends(require_parent)])
async def maintenance_status() -> dict:
    return dict(_state)
