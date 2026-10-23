"""自动排片：按星期目标 + 只听没听过的内容。

规则（按优先级）：
1. **按星期取目标**：周一到周日各自一个分钟数（student.weekday_targets），
   没配置就用 daily_target_min
2. **听过的不再排**：任何"听完"或"累计有效时长过半"的内容都排除，
   防止重复观看；内容不够就少排，不拿旧内容凑数
3. **难度匹配**：优先挑当前级别附近的，不跳级
4. **时长对齐**：按目标分钟装填，允许略超 20%，不硬塞
5. **多样性**：同一系列最多连续 2 集
6. **一周预演**：算未来 7 天时按天顺序模拟，前面排过的不会在后面重复出现

不需要额外的 review 表——"听过"直接从 play_event 汇总出来。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import PlayEvent, Student, Video

TZ = ZoneInfo(settings.tz)

LEVELS = ["L1", "L2", "L3", "L4", "L5"]
MAX_ITEMS = 10                        # 一天最多几集，防止碎片化
OVERSHOOT = 1.2                       # 允许超出目标时长 20%
MAX_SAME_SERIES_IN_A_ROW = 2
LISTENED_RATIO = 0.5                  # 累计有效时长达到总时长的一半就算听过
WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _level_index(level: str | None) -> int | None:
    if not level or level not in LEVELS:
        return None
    return LEVELS.index(level)


def weekday_targets(student: Student) -> list[int]:
    """周一到周日 7 个目标分钟数，缺失的位置用 daily_target_min 兜底。"""
    raw = student.weekday_targets or []
    default = student.daily_target_min or 20
    out = []
    for index in range(7):
        try:
            value = int(raw[index])
        except (IndexError, TypeError, ValueError):
            value = default
        out.append(value if value > 0 else default)
    return out


def target_for_day(student: Student, day: date) -> int:
    """某一天的目标分钟数：按星期取，没配置就退回默认。"""
    if not student.weekday_targets:
        return student.daily_target_min or 20
    return weekday_targets(student)[day.weekday()]


async def _listened_ids(db: AsyncSession, student: Student) -> set[int]:
    """听过就算的集合：听完的，或累计有效时长超过一半的。"""
    rows = (
        await db.execute(
            select(
                PlayEvent.video_id,
                Video.duration_ms,
                func.sum(PlayEvent.effective_ms),
                func.max(PlayEvent.completed),
            )
            .join(Video, Video.id == PlayEvent.video_id)
            .where(PlayEvent.student_id == student.id)
            .group_by(PlayEvent.video_id, Video.duration_ms)
        )
    ).all()

    listened: set[int] = set()
    for video_id, duration_ms, total_ms, completed in rows:
        if completed:
            listened.add(video_id)
        elif duration_ms and (total_ms or 0) >= duration_ms * LISTENED_RATIO:
            listened.add(video_id)
    return listened


async def _candidates(
    db: AsyncSession, student: Student, exclude: set[int]
) -> tuple[list[int], dict[int, tuple[int, int | None]]]:
    """没听过的英语内容，按"级别最接近 → 集数靠前"排序。"""
    rows = (
        await db.execute(
            select(Video.id, Video.level, Video.duration_ms, Video.series_id).where(
                Video.language == "en",
                Video.duration_ms > 0,
            )
        )
    ).all()

    target_index = _level_index(student.level) if student.level in LEVELS else 0
    usable = [row for row in rows if row.id not in exclude and (row.duration_ms or 0) > 0]

    def sort_key(row):
        index = _level_index(row.level)
        distance = 99 if index is None else abs(index - target_index)
        return (distance, index if index is not None else 99, row.id)

    ordered = sorted(usable, key=sort_key)
    meta = {row.id: (row.duration_ms or 0, row.series_id) for row in ordered}
    return [row.id for row in ordered], meta


def _pack(
    candidates: list[int],
    meta: dict[int, tuple[int, int | None]],
    budget_ms: float,
    series_tail: list[int | None],
) -> list[int]:
    """按预算装填，先严格控系列连续性，排不满再放宽。"""
    picked: list[int] = []
    used_ms = 0
    tail = list(series_tail)

    def try_add(video_id: int, enforce_series: bool) -> bool:
        nonlocal used_ms
        if len(picked) >= MAX_ITEMS or used_ms >= budget_ms:
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
        if used_ms >= budget_ms or len(picked) >= MAX_ITEMS:
            break
        try_add(video_id, False)

    return picked


async def build_daily_plan(
    db: AsyncSession,
    student: Student,
    day: date,
    target_min: int | None = None,
    exclude: set[int] | None = None,
) -> dict:
    """生成某一天的计划：只排没听过的内容。

    exclude 用于"一周预演"：前面几天已经排掉的，后面不再重复出现。
    """
    target = target_min if target_min and target_min > 0 else target_for_day(student, day)
    budget_ms = target * 60_000

    listened = await _listened_ids(db, student)
    blocked = listened | (exclude or set())

    ids, meta = await _candidates(db, student, blocked)
    picked = _pack(ids, meta, budget_ms, [])

    total_ms = sum(meta[vid][0] for vid in picked)
    return {
        "target_min": target,
        "level": student.level or LEVELS[0],
        "items": [{"video_id": vid, "kind": "new", "order": i} for i, vid in enumerate(picked)],
        "total_ms": total_ms,
        "new_count": len(picked),
        "review_count": 0,
        "repeat_count": 0,
        "exhausted": len(picked) == 0,
        "remaining": len(ids),
    }


async def build_week(
    db: AsyncSession, student: Student, start_day: date, days: int = 7
) -> list[dict]:
    """未来 N 天的排片预演：逐天模拟，同一条内容不会在两天里重复出现。

    只是"如果照这个节奏排会怎样"，不落库；真正到那天时学生端会落库一份。
    """
    listened = await _listened_ids(db, student)
    blocked = set(listened)
    week: list[dict] = []

    for offset in range(days):
        day = start_day + timedelta(days=offset)
        target = target_for_day(student, day)
        budget_ms = target * 60_000

        ids, meta = await _candidates(db, student, blocked)
        picked = _pack(ids, meta, budget_ms, [])
        blocked |= set(picked)

        total_ms = sum(meta[vid][0] for vid in picked)
        week.append(
            {
                "day": day.isoformat(),
                "weekday": WEEKDAYS_CN[day.weekday()],
                "target_min": target,
                "items": [
                    {
                        "video_id": vid,
                        "title": None,      # 由调用方补
                        "duration_ms": meta[vid][0],
                        "order": index,
                    }
                    for index, vid in enumerate(picked)
                ],
                "total_min": round(total_ms / 60_000, 1),
                "short": total_ms < budget_ms * 0.8,   # 内容不够
            }
        )

    return week
