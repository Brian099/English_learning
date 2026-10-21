from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import DailyStat, Device, Plan, PlanItem, PlayEvent, Student, Video
from ..scheduler import build_daily_plan
from ..security import current_device

router = APIRouter(prefix="/api", tags=["student"])

TZ = ZoneInfo(settings.tz)


def _today() -> date:
    return datetime.now(TZ).date()


@router.get("/me")
async def me(device: Device = Depends(current_device), db: AsyncSession = Depends(get_db)) -> dict:
    """孩子端确认"我是谁"，配对成功后第一件事就调它。"""
    student = await db.get(Student, device.student_id)
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "学生不存在")
    return {
        "student": {"id": student.id, "name": student.name, "daily_target_min": student.daily_target_min},
        "device": {"id": device.id, "name": device.name, "platform": device.platform},
    }


@router.get("/student/today")
async def student_today(
    device: Device = Depends(current_device), db: AsyncSession = Depends(get_db)
) -> dict:
    """孩子端首页：今天要听什么 + 已经听了多少。

    当天没有计划时**自动排片并落库**——家长不用每天手动挑，
    打开就是一份"复习到期 + 难度匹配 + 时长装填"的清单。
    """
    student = await db.get(Student, device.student_id)
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "学生不存在")

    day = _today()
    plan = (
        await db.execute(select(Plan).where(Plan.student_id == student.id, Plan.day == day))
    ).scalar_one_or_none()

    # 没计划就自动生成一份（家长之后可以在网页上改，改了就不再自动覆盖）
    if plan is None:
        built = await build_daily_plan(db, student, day)
        if built["items"]:
            plan = Plan(
                student_id=student.id,
                day=day,
                target_min=built["target_min"],
                source="auto",
                note=f"自动排片：复习 {built['review_count']} · 新内容 {built['new_count']}"
                     f" · 重复 {built['repeat_count']}",
            )
            db.add(plan)
            await db.flush()
            for item in built["items"]:
                db.add(
                    PlanItem(
                        plan_id=plan.id,
                        video_id=item["video_id"],
                        order_index=item["order"],
                        kind=item["kind"],
                    )
                )
            await db.commit()
            await db.refresh(plan)

    items: list[dict] = []
    if plan is not None:
        rows = (
            await db.execute(
                select(PlanItem, Video)
                .join(Video, Video.id == PlanItem.video_id)
                .where(PlanItem.plan_id == plan.id)
                .order_by(PlanItem.order_index)
            )
        ).all()
        for item, video in rows:
            items.append(
                {
                    "video_id": video.id,
                    "title": video.title,
                    "duration_ms": video.duration_ms,
                    "speech_ms": video.speech_ms,
                    "level": video.level,
                    "kind": item.kind,
                    "order": item.order_index,
                }
            )
    else:
        # 兜底：视频库太小或全部听完，退回按难度排序
        videos = (
            await db.execute(
                select(Video).order_by(Video.level, Video.id).limit(20)
            )
        ).scalars().all()
        for video in videos:
            items.append(
                {
                    "video_id": video.id,
                    "title": video.title,
                    "duration_ms": video.duration_ms,
                    "speech_ms": video.speech_ms,
                    "level": video.level,
                    "kind": "new",
                    "order": len(items),
                }
            )

    # 今天已经完成的集数
    start = datetime.combine(day, datetime.min.time(), TZ)
    completed_rows = (
        await db.execute(
            select(PlayEvent.video_id)
            .where(
                PlayEvent.student_id == student.id,
                PlayEvent.started_at >= start,
                PlayEvent.completed.is_(True),
            )
            .distinct()
        )
    ).scalars().all()
    completed_ids = set(completed_rows)

    for item in items:
        item["completed"] = item["video_id"] in completed_ids
        item["audio_url"] = f"/api/media/{item['video_id']}?track=audio"
        item["video_url"] = f"/api/media/{item['video_id']}"

    stat = await db.get(DailyStat, (student.id, day))
    effective_min = round((stat.effective_ms if stat else 0) / 60000, 1)
    target_min = plan.target_min if plan else student.daily_target_min

    return {
        "student": {"id": student.id, "name": student.name},
        "day": day.isoformat(),
        "target_min": target_min,
        "effective_min": effective_min,
        "completed_count": stat.completed_count if stat else 0,
        "has_plan": plan is not None,
        "plan_source": plan.source if plan else None,
        "items": items,
    }


@router.get("/student/history")
async def student_history(
    days: int = 7,
    device: Device = Depends(current_device),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """孩子端的"我这几天"——简单几根柱子，不做复杂报表。"""
    student = await db.get(Student, device.student_id)
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "学生不存在")

    today = _today()
    start = today - timedelta(days=days - 1)
    rows = (
        await db.execute(
            select(DailyStat).where(
                DailyStat.student_id == student.id,
                DailyStat.day >= start,
                DailyStat.day <= today,
            )
        )
    ).scalars().all()
    by_day = {row.day: row for row in rows}

    series = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        stat = by_day.get(day)
        series.append(
            {
                "day": day.isoformat(),
                "effective_min": round((stat.effective_ms if stat else 0) / 60000, 1),
                "completed_count": stat.completed_count if stat else 0,
            }
        )

    total = round(sum(item["effective_min"] for item in series), 1)
    return {"days": series, "total_min": total, "active_days": sum(1 for i in series if i["effective_min"] > 0)}
