from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import DailyStat, Plan, PlayEvent, Student, Video
from ..security import require_parent

router = APIRouter(prefix="/api", tags=["report"])

TZ = ZoneInfo(settings.tz)


def _today() -> date:
    return datetime.now(TZ).date()


async def _student(db: AsyncSession, student_id: int) -> Student:
    student = await db.get(Student, student_id)
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "学生不存在")
    return student


async def _streak(db: AsyncSession, student_id: int) -> int:
    """连续天数：从今天（或昨天）往前数，有效时长 > 0 的天。"""
    rows = (
        await db.execute(
            select(DailyStat.day)
            .where(DailyStat.student_id == student_id, DailyStat.effective_ms > 0)
            .order_by(DailyStat.day.desc())
            .limit(400)
        )
    ).scalars().all()
    if not rows:
        return 0

    today = _today()
    if rows[0] not in (today, today - timedelta(days=1)):
        return 0

    streak = 1
    cursor = rows[0]
    for day in rows[1:]:
        if day == cursor - timedelta(days=1):
            streak += 1
            cursor = day
        else:
            break
    return streak


@router.get("/dashboard", dependencies=[Depends(require_parent)])
async def dashboard(db: AsyncSession = Depends(get_db)) -> dict:
    """家长首页一次性拿全部学生概览，避免前端多次往返。"""
    students = (
        await db.execute(select(Student).where(Student.active.is_(True)).order_by(Student.id))
    ).scalars().all()

    today = _today()
    start = today - timedelta(days=6)

    stats = (
        await db.execute(
            select(DailyStat).where(DailyStat.day >= start, DailyStat.day <= today)
        )
    ).scalars().all()
    by_key: dict[tuple[int, date], DailyStat] = {(s.student_id, s.day): s for s in stats}

    cards = []
    for student in students:
        day_rows = []
        for offset in range(7):
            day = start + timedelta(days=offset)
            stat = by_key.get((student.id, day))
            day_rows.append(
                {
                    "day": day.isoformat(),
                    "effective_min": round((stat.effective_ms if stat else 0) / 60000, 1),
                    "completed_count": stat.completed_count if stat else 0,
                }
            )

        today_stat = by_key.get((student.id, today))
        plan = (
            await db.execute(select(Plan).where(Plan.student_id == student.id, Plan.day == today))
        ).scalar_one_or_none()
        target = plan.target_min if plan else student.daily_target_min
        effective_min = round((today_stat.effective_ms if today_stat else 0) / 60000, 1)

        total_week = round(sum(d["effective_min"] for d in day_rows), 1)
        cards.append(
            {
                "student": {"id": student.id, "name": student.name},
                "today": {
                    "day": today.isoformat(),
                    "effective_min": effective_min,
                    "target_min": target,
                    "reached": effective_min >= target,
                    "completed_count": today_stat.completed_count if today_stat else 0,
                },
                "week": {
                    "days": day_rows,
                    "total_min": total_week,
                    "active_days": sum(1 for d in day_rows if d["effective_min"] > 0),
                },
                "streak": await _streak(db, student.id),
            }
        )

    return {"day": today.isoformat(), "students": cards}


@router.get("/report/{student_id}/today", dependencies=[Depends(require_parent)])
async def report_today(student_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    student = await _student(db, student_id)
    day = _today()

    stat = await db.get(DailyStat, (student_id, day))
    plan = (
        await db.execute(select(Plan).where(Plan.student_id == student_id, Plan.day == day))
    ).scalar_one_or_none()

    target_min = plan.target_min if plan else student.daily_target_min
    effective_min = round((stat.effective_ms if stat else 0) / 60000, 1)

    return {
        "student": {"id": student.id, "name": student.name},
        "day": day.isoformat(),
        "effective_min": effective_min,
        "target_min": target_min,
        "reached": effective_min >= target_min,
        "completed_count": stat.completed_count if stat else 0,
        "session_count": stat.session_count if stat else 0,
        "streak": await _streak(db, student_id),
    }


@router.get("/report/{student_id}/week", dependencies=[Depends(require_parent)])
async def report_week(student_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    student = await _student(db, student_id)
    today = _today()
    start = today - timedelta(days=6)

    rows = (
        await db.execute(
            select(DailyStat).where(
                DailyStat.student_id == student_id, DailyStat.day >= start, DailyStat.day <= today
            )
        )
    ).scalars().all()
    by_day = {row.day: row for row in rows}

    days = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        stat = by_day.get(day)
        days.append(
            {
                "day": day.isoformat(),
                "effective_min": round((stat.effective_ms if stat else 0) / 60000, 1),
                "completed_count": stat.completed_count if stat else 0,
            }
        )

    total_min = round(sum(d["effective_min"] for d in days), 1)
    active_days = sum(1 for d in days if d["effective_min"] > 0)

    return {
        "student": {"id": student.id, "name": student.name},
        "days": days,
        "total_min": total_min,
        "avg_min": round(total_min / 7, 1),
        "active_days": active_days,
        "streak": await _streak(db, student_id),
    }


@router.get("/report/{student_id}/attention", dependencies=[Depends(require_parent)])
async def report_attention(student_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """关注项（不是警告）：只听了个开头就退出的集数、本周空白日。"""
    student = await _student(db, student_id)
    today = _today()
    since = datetime.combine(today - timedelta(days=6), datetime.min.time(), TZ)

    events = (
        await db.execute(
            select(PlayEvent, Video)
            .join(Video, Video.id == PlayEvent.video_id)
            .where(PlayEvent.student_id == student_id, PlayEvent.started_at >= since)
            .order_by(PlayEvent.started_at.desc())
            .limit(500)
        )
    ).all()

    short_sessions: dict[int, dict] = {}
    for event, video in events:
        if not video.duration_ms or video.duration_ms <= 0:
            continue
        ratio = event.max_position_ms / video.duration_ms
        if event.effective_ms >= 10_000 and ratio < 0.3:
            entry = short_sessions.setdefault(
                video.id,
                {
                    "video_id": video.id,
                    "title": video.title,
                    "duration_min": round(video.duration_ms / 60000, 1),
                    "count": 0,
                    "last_at": event.started_at.isoformat(),
                },
            )
            entry["count"] += 1

    items = [v for v in short_sessions.values() if v["count"] >= 2]

    days = (
        await db.execute(
            select(DailyStat.day).where(
                DailyStat.student_id == student_id,
                DailyStat.day >= today - timedelta(days=6),
                DailyStat.effective_ms > 0,
            )
        )
    ).scalars().all()
    active = set(days)
    empty_days = [
        (today - timedelta(days=offset)).isoformat()
        for offset in range(7)
        if (today - timedelta(days=offset)) not in active
    ]

    return {
        "student": {"id": student.id, "name": student.name},
        "short_sessions": items,
        "empty_days": empty_days,
    }
