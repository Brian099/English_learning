import asyncio
import hashlib
import os
import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .media_tools import ffprobe
from .models import Series, Video

VIDEO_EXT = {".mp4", ".mkv", ".m4v", ".mov", ".webm", ".avi", ".flv", ".ts", ".wmv"}


async def _duration_ms(path: Path) -> int | None:
    """用 ffprobe 取时长；失败返回 None，不阻塞扫描。

    注意：这里用 subprocess.run + to_thread，不用 asyncio.create_subprocess_exec。
    后者在 Windows 上走命名管道，受限环境（沙箱/某些容器）会直接 PermissionError，
    而且异常被吞掉时表现为"时长永远是 0"，很难查。
    """
    executable = ffprobe()
    if executable is None:
        return None

    def _run() -> int | None:
        try:
            result = subprocess.run(
                [executable, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            text = (result.stdout or "").strip()
            return int(float(text) * 1000) if text else None
        except Exception:
            return None

    return await asyncio.to_thread(_run)


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
            # 时长为 0 说明上次探测失败过，即使文件没变也要重探一次
            if (existing and existing.size_bytes == stat.st_size
                    and existing.mtime == int(stat.st_mtime)
                    and existing.duration_ms):
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

            duration = await _duration_ms(path)
            values = {
                "title": Path(name).stem,
                "series_id": series.id,
                # ffprobe 失败时不要写 None（列是非空的）；更新已有记录时保留旧时长
                "duration_ms": duration if duration is not None else (existing.duration_ms if existing else 0),
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
