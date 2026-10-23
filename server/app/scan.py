import asyncio
import hashlib
import os
import subprocess
from pathlib import Path

from sqlalchemy import func, select
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
                capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
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


def _iter_video_files(root: Path):
    """遍历媒体目录，产出 (相对路径, 绝对路径, stat)。"""
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() not in VIDEO_EXT:
                continue
            yield path.relative_to(root).as_posix(), path, path.stat()


async def find_missing(db: AsyncSession) -> list[tuple[int, str, str]]:
    """库里登记了、但磁盘上已经找不到的视频。

    返回 [(video_id, rel_path, title)]。
    """
    root = Path(settings.media_root)
    rows = (await db.execute(select(Video.id, Video.rel_path, Video.title))).all()
    missing = []
    for row in rows:
        if not (root / row.rel_path).is_file():
            missing.append((row.id, row.rel_path, row.title))
    return missing


def _purge_cache_files(video_id: int) -> None:
    """删掉这一集在 cache 里的派生物：音轨、ASR 输入、归一化视频。"""
    for sub, suffix in (("audio", ".m4a"), ("asr", ".wav"), ("video", ".mp4")):
        path = Path(settings.cache_root) / sub / f"{video_id}{suffix}"
        if not path.is_file():
            continue
        try:
            path.unlink()
        except OSError:
            pass


async def purge_missing(db: AsyncSession, missing: list[tuple[int, str, str]]) -> list[dict]:
    """把缺失的视频彻底清掉。

    数据库记录删除后，transcript / play_event / plan_item 靠外键级联一起删；
    cache 里的音轨等派生物单独清理；没有视频的系列也一并删除。
    """
    removed = []
    for video_id, rel_path, title in missing:
        _purge_cache_files(video_id)
        video = await db.get(Video, video_id)
        if video is not None:
            await db.delete(video)
        removed.append({"video_id": video_id, "rel_path": rel_path, "title": title})

    if removed:
        await db.flush()
        # 清掉已经没有视频的空系列
        series_ids = (await db.execute(select(Series.id))).scalars().all()
        for series_id in series_ids:
            remaining = (
                await db.execute(
                    select(func.count(Video.id)).where(Video.series_id == series_id)
                )
            ).scalar_one()
            if remaining == 0:
                series = await db.get(Series, series_id)
                if series is not None:
                    await db.delete(series)

    return removed


async def scan_preview(db: AsyncSession) -> dict:
    """只检测，不写库：告诉你有多少文件、多少是新的、多少变了、多少已经没了。

    这一步不动数据库，也不会触发任何转码/转写。
    """
    root = Path(settings.media_root)
    if not root.is_dir():
        return {"error": f"媒体目录不存在: {root}", "total": 0,
                "new": 0, "changed": 0, "unchanged": 0, "series": {}}

    existing_rows = (
        await db.execute(select(Video.rel_path, Video.size_bytes, Video.mtime, Video.duration_ms))
    ).all()
    index = {row.rel_path: row for row in existing_rows}

    total = new = changed = unchanged = 0
    series: dict[str, dict[str, int]] = {}
    new_examples: list[str] = []

    for rel, _path, stat in _iter_video_files(root):
        total += 1
        series_name = rel.split("/")[0] if "/" in rel else "未分类"
        bucket = series.setdefault(series_name, {"total": 0, "new": 0, "changed": 0})
        bucket["total"] += 1

        row = index.get(rel)
        if row is None:
            new += 1
            bucket["new"] += 1
            if len(new_examples) < 8:
                new_examples.append(rel)
        elif (row.size_bytes != stat.st_size or row.mtime != int(stat.st_mtime)
              or not row.duration_ms):
            changed += 1
            bucket["changed"] += 1
        else:
            unchanged += 1

    pending = new + changed
    missing = await find_missing(db)
    return {
        "media_root": str(root),
        "total": total,
        "new": new,
        "changed": changed,
        "unchanged": unchanged,
        "pending": pending,
        # 每集约 1–1.5 分钟（音量测量 + 抽音轨 + 转写）
        "estimated_minutes": round(pending * 1.2, 1),
        # 库里有记录但磁盘上已经没有的文件（导入时会被彻底清理）
        "missing": len(missing),
        "missing_examples": [{"title": title, "rel_path": rel} for _, rel, title in missing[:8]],
        "series": series,
        "new_examples": new_examples,
    }


async def scan_media(db: AsyncSession) -> dict:
    """增量扫描 MEDIA_ROOT：新增入库、变更更新、无变化跳过。"""
    root = Path(settings.media_root)
    if not root.is_dir():
        return {"error": f"media root not found: {root}", "added": 0, "updated": 0, "skipped": 0}

    added = updated = skipped = 0
    touched: list[Video] = []

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
                touched.append(existing)
            else:
                created = Video(rel_path=rel, **values)
                db.add(created)
                added += 1
                touched.append(created)

    await db.commit()
    # 返回新增/变更的视频 id，供入库流水线接着跑（转音频 + 提取对白）
    ids = []
    for video in touched:
        await db.refresh(video)
        ids.append(video.id)

    # 清理"库里有、磁盘上没有"的记录（连同转写、播放记录、计划项、cache 音轨）
    missing = await find_missing(db)
    removed: list[dict] = []
    warning = None
    if missing:
        # 安全阀：媒体目录一个视频都没扫到、而库里全是缺失
        # 这种情况八成是挂载掉了，不能把库清空
        if (added + updated + skipped) == 0:
            warning = (f"媒体目录里一个视频都没找到，但库里有 {len(missing)} 条记录，"
                       f"疑似挂载异常，已跳过清理")
        else:
            removed = await purge_missing(db, missing)
            await db.commit()

    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "ids": ids,
        "removed": len(removed),
        "removed_items": removed[:20],
        "warning": warning,
    }
