import asyncio
import hashlib
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import Series, Video

VIDEO_EXT = {".mp4", ".mkv", ".m4v", ".mov", ".webm", ".avi", ".flv", ".ts", ".wmv"}


async def _duration_ms(path: Path) -> int | None:
    """用 ffprobe 取时长；失败返回 None，不阻塞扫描。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        text = out.decode().strip()
        return int(float(text) * 1000) if text else None
    except Exception:
        return None


def _quick_hash(path: Path, size: int) -> str:
    """首尾 64KB + 文件大小，够用于去重，不做全文件哈希。"""
    h = hashlib.sha256()
    h.update(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(65536))
        if size > 131072:
            f.seek(-65536, os.SEEK_END)
            h.update(f.read(65536))
    return h.hexdigest()[:32]


async def scan_media(db: AsyncSession) -> dict:
    """增量扫描 MEDIA_ROOT：新增入库、变更更新、无变化跳过。"""
    root = Path(settings.media_root)
    if not root.is_dir():
        return {"error": f"media root not found: {root}", "added": 0, "updated": 0, "skipped": 0}

    added = updated = skipped = 0

    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() not in VIDEO_EXT:
                continue

            rel = path.relative_to(root).as_posix()
            stat = path.stat()

            existing = (
                await db.execute(select(Video).where(Video.rel_path == rel))
            ).scalar_one_or_none()
            if existing and existing.size_bytes == stat.st_size and existing.mtime == int(stat.st_mtime):
                skipped += 1
                continue

            series_name = rel.split("/")[0] if "/" in rel else "未分类"
            series = (
                await db.execute(select(Series).where(Series.name == series_name))
            ).scalar_one_or_none()
            if series is None:
                series = Series(name=series_name)
                db.add(series)
                await db.flush()

            values = {
                "title": Path(name).stem,
                "series_id": series.id,
                "duration_ms": await _duration_ms(path),
                "size_bytes": stat.st_size,
                "mtime": int(stat.st_mtime),
                "file_hash": _quick_hash(path, stat.st_size),
            }
            if existing:
                for key, value in values.items():
                    setattr(existing, key, value)
                updated += 1
            else:
                db.add(Video(rel_path=rel, **values))
                added += 1

    await db.commit()
    return {"added": added, "updated": updated, "skipped": skipped}
