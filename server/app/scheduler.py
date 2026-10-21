"""自动排片：给某个学生生成某一天的学习计划。

设计原则（按优先级）：
1. **复习优先**：听过的内容按 1/3/7/15/30 天的间隔到期复习，到期的先排
2. **难度匹配**：新内容挑"当前级别 ±1"的，不跳级；当前级别由最近完成的内容推断
3. **时长对齐**：按目标分钟数装填，允许略超 20%，但不硬塞
4. **多样性**：同一系列连续不超过 2 集，避免一天听腻
5. **不重复**：当天已完成的集不再排

不需要额外的 review_item 表——到期判断直接从 play_event 的完成记录算出来。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import PlayEvent, Series, Student, Video

TZ = ZoneInfo(settings.tz)

LEVELS = ["L1", "L2", "L3", "L4", "L5"]
REVIEW_GAPS_DAYS = [1, 3, 7, 15, 30]   # 第 n 次完成后的复习间隔
REVIEW_SHARE = 0.4                    # 目标时长的 40% 留给复习
MAX_ITEMS = 8                         # 一天最多几集，防止碎片化
OVERSHOOT = 1.2                       # 允许超出目标时长 20%
MAX_SAME_SERIES_IN_A_ROW = 2


def _as_date(moment: datetime | None) -> date | None:
    if moment is None:
        return None
    if moment.tzinfo is None:      # SQLite 读回来不带时区，按 UTC 理解
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    return moment.astimezone(TZ).date()


def _level_index(level: str | None) -> int | None:
    if not level or level not in LEVELS:
        return None
    return LEVELS.index(level)


async def _current_level(db: AsyncSession, student: Student, day: date) -> str:
    """推断学生当前级别：最近 5 次完成内容的级别中位数；没有历史就从最低级别开始。"""
    if student.level in LEVELS:
        return student.level

    rows = (
        await db.execute(
            select(Video.level)
            .join(PlayEvent, PlayEvent.video_id == Video.id)
            .where(
                PlayEvent.student_id == student.id,
                PlayEvent.completed.is_(True),
                PlayEvent.started_at < datetime.combine(day, datetime.min.time(), TZ),
                Video.level.is_not(None),
            )
            .order_by(PlayEvent.started_at.desc())
            .limit(5)
        )
    ).scalars().all()

    indices = sorted(i for i in (_level_index(lv) for lv in rows) if i is not None)
    if not indices:
        return LEVELS[0]
    return LEVELS[indices[len(indices) // 2]]


async def _review_queue(db: AsyncSession, student: Student, day: date) -> list[int]:
    """到期该复习的视频，按到期日从早到晚。"""
    rows = (
        await db.execute(
            select(
                PlayEvent.video_id,
                func.count(PlayEvent.id),
                func.max(PlayEvent.started_at),
            )
            .where(PlayEvent.student_id == student.id, PlayEvent.completed.is_(True))
            .group_by(PlayEvent.video_id)
        )
    ).all()

    due: list[tuple[int, date]] = []
    for video_id, count, last_at in rows:
        last_day = _as_date(last_at)
        if last_day is None:
            continue
        gap = REVIEW_GAPS_DAYS[min(max(count, 1) - 1, len(REVIEW_GAPS_DAYS) - 1)]
        due_day = last_day + timedelta(days=gap)
        if due_day <= day:
            due.append((video_id, due_day))

    due.sort(key=lambda item: (item[1], item[0]))
    return [video_id for video_id, _ in due]


async def _new_queue(db: AsyncSession, student: Student, level: str) -> list[int]:
    """没听过的新内容，按"级别最接近 + 集数靠前"排序。"""
    heard = select(PlayEvent.video_id).where(PlayEvent.student_id == student.id).scalar_subquery()
    videos = (
        await db.execute(
            select(Video.id, Video.level, Video.duration_ms, Video.series_id)
            .where(
                Video.id.not_in(heard),
                Video.language == "en",
                Video.duration_ms > 0,
            )
        )
    ).all()

    target_index = _level_index(level) or 0

    def sort_key(row):
        index = _level_index(row.level)
        distance = 99 if index is None else abs(index - target_index)
        # 同距离时优先更简单的，避免一上来就上难度
        return (distance, index if index is not None else 99, row.id)

    return [row.id for row in sorted(videos, key=sort_key)]


def _pack(
    candidates: list[int],
    meta: dict[int, tuple[int, int | None]],   # video_id -> (duration_ms, series_id)
    budget_ms: float,
    already: list[int],
    series_tail: list[int | None],
) -> list[int]:
    """按预算装填。

    先按"同系列最多连续 2 集"的严格规则排一遍，排不满预算再放宽——
    视频库只有一两个系列时，严格规则会把计划饿死（实测只排到目标的 40%）。
    """
    picked: list[int] = []
    used_ms = sum(meta.get(vid, (0, None))[0] for vid in already)
    tail = list(series_tail)

    def try_add(video_id: int, enforce_series: bool) -> bool:
        nonlocal used_ms
        if len(already) + len(picked) >= MAX_ITEMS or used_ms >= budget_ms:
            return False
        duration_ms, series_id = meta.get(video_id, (0, None))
        if duration_ms <= 0:
            return False
        if (enforce_series and series_id is not None
                and len(tail) >= MAX_SAME_SERIES_IN_A_ROW
                and all(sid == series_id for sid in tail[-MAX_SAME_SERIES_IN_A_ROW:])):
            return False
        # 会明显超出预算的单集，只在还没排到任何内容时才接受
        if used_ms > 0 and used_ms + duration_ms > budget_ms * OVERSHOOT:
            return False
        picked.append(video_id)
        used_ms += duration_ms
        tail.append(series_id)
        return True

    for video_id in candidates:          # 严格：控制系列连续性
        try_add(video_id, True)
    for video_id in candidates:          # 放宽：把预算填满
        if video_id in picked:
            continue
        if used_ms >= budget_ms or len(already) + len(picked) >= MAX_ITEMS:
            break
        try_add(video_id, False)

    return picked


async def _fill_queue(
    db: AsyncSession, student: Student, day: date, exclude: set[int]
) -> list[int]:
    """补足队列：没到期但听过的内容，按"最久没听"排序。

    磨耳朵的核心就是重复，所以内容不够时重复听旧的是合理的，
    而不是让当天计划只有两分钟。
    """
    start = datetime.combine(day, datetime.min.time(), TZ)
    rows = (
        await db.execute(
            select(PlayEvent.video_id, func.max(PlayEvent.started_at))
            .where(
                PlayEvent.student_id == student.id,
                PlayEvent.started_at < start,          # 今天听过的排除
            )
            .group_by(PlayEvent.video_id)
        )
    ).all()

    candidates = [
        (video_id, _as_date(last_at))
        for video_id, last_at in rows
        if video_id not in exclude
    ]
    candidates.sort(key=lambda item: (item[1] or date.min, item[0]))
    return [video_id for video_id, _ in candidates]


async def build_daily_plan(
    db: AsyncSession, student: Student, day: date, target_min: int | None = None
) -> dict:
    """生成一天的计划，返回 {target_min, items:[{video_id, kind, order}]}。"""
    target = target_min if target_min and target_min > 0 else student.daily_target_min
    budget_ms = target * 60_000

    level = await _current_level(db, student, day)
    review_ids = await _review_queue(db, student, day)
    new_ids = await _new_queue(db, student, level)

    # 今天已经完成的集不排
    start = datetime.combine(day, datetime.min.time(), TZ)
    end = start + timedelta(days=1)
    done_today = set(
        (
            await db.execute(
                select(PlayEvent.video_id).where(
                    PlayEvent.student_id == student.id,
                    PlayEvent.started_at >= start,
                    PlayEvent.started_at < end,
                    PlayEvent.completed.is_(True),
                )
            )
        ).scalars().all()
    )
    review_ids = [vid for vid in review_ids if vid not in done_today]
    new_ids = [vid for vid in new_ids if vid not in done_today]

    # 取元数据
    all_ids = list(dict.fromkeys(review_ids + new_ids))
    meta: dict[int, tuple[int, int | None]] = {}
    if all_ids:
        rows = (
            await db.execute(
                select(Video.id, Video.duration_ms, Video.series_id).where(Video.id.in_(all_ids))
            )
        ).all()
        meta = {row.id: (row.duration_ms or 0, row.series_id) for row in rows}

    # 复习占 40% 预算，剩下给新内容
    review_budget = budget_ms * REVIEW_SHARE
    review_picked = _pack(review_ids, meta, review_budget, [], [])

    series_tail = [meta[vid][1] for vid in review_picked]
    new_picked = _pack(new_ids, meta, budget_ms, review_picked, series_tail)

    # 新内容不够就补足：重复听旧内容（磨耳朵本来就靠重复）
    fill_picked: list[int] = []
    used = sum(meta[vid][0] for vid in review_picked + new_picked)
    if used < budget_ms * 0.8:
        picked_ids = set(review_picked + new_picked)
        fill_ids = await _fill_queue(db, student, day, picked_ids)
        if fill_ids:
            rows = (
                await db.execute(
                    select(Video.id, Video.duration_ms, Video.series_id).where(Video.id.in_(fill_ids))
                )
            ).all()
            for row in rows:
                meta.setdefault(row.id, (row.duration_ms or 0, row.series_id))
            series_tail = [meta[vid][1] for vid in review_picked + new_picked]
            fill_picked = _pack(fill_ids, meta, budget_ms, review_picked + new_picked, series_tail)

    items = (
        [{"video_id": vid, "kind": "review", "order": i} for i, vid in enumerate(review_picked)]
        + [{"video_id": vid, "kind": "new", "order": len(review_picked) + i}
           for i, vid in enumerate(new_picked)]
        + [{"video_id": vid, "kind": "repeat",
            "order": len(review_picked) + len(new_picked) + i}
           for i, vid in enumerate(fill_picked)]
    )

    total_ms = sum(meta[vid][0] for vid in review_picked + new_picked + fill_picked)

    return {
        "target_min": target,
        "level": level,
        "items": items,
        "total_ms": total_ms,
        "review_count": len(review_picked),
        "new_count": len(new_picked),
        "repeat_count": len(fill_picked),
    }
