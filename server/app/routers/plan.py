from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Plan, PlanItem, Student, Video
from ..scheduler import WEEKDAYS_CN, build_daily_plan, build_week, target_for_day
from ..security import read_auth, require_parent

router = APIRouter(prefix="/api", tags=["plan"])

TZ = ZoneInfo(settings.tz)


def _today() -> date:
    return datetime.now(TZ).date()


class PlanBody(BaseModel):
    target_min: int = Field(default=20, ge=0, le=180)
    note: str | None = None
    video_ids: list[int] = Field(default_factory=list)


@router.get("/schedule/{student_id}")
async def week_schedule(
    student_id: int,
    days: int = Query(7, ge=1, le=14),
    start: date | None = None,
    _auth: dict = Depends(read_auth),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """未来 N 天的排片预览（默认从今天起 7 天）。

    今天优先用已落库的计划（学生端看到的就是这份），
    以后的日子按同样规则预演，同一条内容不会在两天里重复。
    """
    student = await db.get(Student, student_id)
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "学生不存在")

    start_day = start or _today()
    week = await build_week(db, student, start_day, days)

    # 补标题
    all_ids = {item["video_id"] for day in week for item in day["items"]}
    titles: dict[int, tuple[str, str | None]] = {}
    if all_ids:
        rows = (
            await db.execute(
                select(Video.id, Video.title, Video.level).where(Video.id.in_(all_ids))
            )
        ).all()
        titles = {row.id: (row.title, row.level) for row in rows}

    # 今天若有落库的计划，用它覆盖（学生端实际看到的那份）
    today = _today()
    saved = (
        await db.execute(select(Plan).where(Plan.student_id == student_id, Plan.day == today))
    ).scalar_one_or_none()
    if saved is not None:
        rows = (
            await db.execute(
                select(PlanItem, Video)
                .join(Video, Video.id == PlanItem.video_id)
                .where(PlanItem.plan_id == saved.id)
                .order_by(PlanItem.order_index)
            )
        ).all()
        for day in week:
            if day["day"] == today.isoformat():
                day["target_min"] = saved.target_min
                day["items"] = [
                    {
                        "video_id": video.id,
                        "title": video.title,
                        "level": video.level,
                        "duration_ms": video.duration_ms,
                        "order": item.order_index,
                    }
                    for item, video in rows
                ]
                day["total_min"] = round(
                    sum(v.duration_ms or 0 for _, v in rows) / 60_000, 1
                )
                day["saved"] = True

    for day in week:
        day.setdefault("saved", False)
        for item in day["items"]:
            if item.get("title") is None:
                title, level = titles.get(item["video_id"], ("（已删除）", None))
                item["title"] = title
                item["level"] = level
        day["weekday"] = WEEKDAYS_CN[date.fromisoformat(day["day"]).weekday()]
        day["is_today"] = day["day"] == today.isoformat()

    return {
        "student_id": student_id,
        "start": start_day.isoformat(),
        "weekday_targets": _weekday_targets_of(student),
        "days": week,
    }


def _weekday_targets_of(student: Student) -> list[int]:
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


def _set_weekday_target(student: Student, day: date, value: int) -> None:
    """把某一天的新目标写进对应星期的那一格。"""
    targets = list(student.weekday_targets or [student.daily_target_min or 20] * 7)
    if len(targets) < 7:
        targets += [student.daily_target_min or 20] * (7 - len(targets))
    targets[day.weekday()] = value
    student.weekday_targets = targets


@router.get("/plans/{student_id}/{day}")
async def get_plan(
    student_id: int,
    day: date,
    _auth: dict = Depends(read_auth),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """孩子端拉取今日任务。"""
    plan = (
        await db.execute(select(Plan).where(Plan.student_id == student_id, Plan.day == day))
    ).scalar_one_or_none()
    if plan is None:
        return {"day": day.isoformat(), "target_min": None, "items": []}

    rows = (
        await db.execute(
            select(PlanItem, Video)
            .join(Video, Video.id == PlanItem.video_id)
            .where(PlanItem.plan_id == plan.id)
            .order_by(PlanItem.order_index)
        )
    ).all()

    return {
        "day": day.isoformat(),
        "target_min": plan.target_min,
        "note": plan.note,
        "source": plan.source,
        "items": [
            {
                "video_id": video.id,
                "title": video.title,
                "duration_ms": video.duration_ms,
                "kind": item.kind,
                "order": item.order_index,
                "media_url": f"/api/media/{video.id}",
            }
            for item, video in rows
        ],
    }


@router.post("/plans/{student_id}/{day}/auto", dependencies=[Depends(require_parent)])
async def auto_plan(
    student_id: int,
    day: date,
    target_min: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """自动排片：按当天的星期目标 + 难度匹配 + 时长装填，只排没听过的内容。"""
    student = await db.get(Student, student_id)
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "学生不存在")

    built = await build_daily_plan(db, student, day, target_min)
    if not built["items"]:
        return {
            "day": day.isoformat(),
            "target_min": built["target_min"],
            "level": built["level"],
            "source": "auto",
            "items": [],
            "message": "没有可排的内容（视频都听过了，或视频库为空）",
        }

    plan = (
        await db.execute(select(Plan).where(Plan.student_id == student_id, Plan.day == day))
    ).scalar_one_or_none()
    if plan is None:
        plan = Plan(student_id=student_id, day=day)
        db.add(plan)
        await db.flush()
    plan.target_min = built["target_min"]
    plan.source = "auto"
    # 家长显式指定了目标，就写回该星期的那一格，保证设置页/概览页/学生端一致
    if target_min:
        _set_weekday_target(student, day, built["target_min"])
        student.daily_target_min = built["target_min"]
    plan.note = f"自动排片：{built['new_count']} 集 · {WEEKDAYS_CN[day.weekday()]}目标 {built['target_min']} 分钟"

    await db.execute(delete(PlanItem).where(PlanItem.plan_id == plan.id))
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

    total_min = round(built["total_ms"] / 60000, 1)
    message = None
    if total_min < built["target_min"] * 0.8:
        message = (f"内容不足：库里只够排 {total_min} 分钟（目标 {built['target_min']} 分钟），"
                   f"建议补充视频或调低每日目标")

    return {
        "day": day.isoformat(),
        "target_min": built["target_min"],
        "level": built["level"],
        "source": "auto",
        "total_min": total_min,
        "review_count": built["review_count"],
        "new_count": built["new_count"],
        "repeat_count": built["repeat_count"],
        "items": len(built["items"]),
        "message": message,
    }


@router.put("/plans/{student_id}/{day}", dependencies=[Depends(require_parent)])
async def put_plan(
    student_id: int, day: date, body: PlanBody, db: AsyncSession = Depends(get_db)
) -> dict:
    """家长端下发计划：整份覆盖该天的 items。"""
    student = await db.get(Student, student_id)
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "学生不存在")

    if body.video_ids:
        known = set(
            (await db.execute(select(Video.id).where(Video.id.in_(body.video_ids)))).scalars().all()
        )
        missing = [vid for vid in body.video_ids if vid not in known]
        if missing:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"视频不存在: {missing}")

    plan = (
        await db.execute(select(Plan).where(Plan.student_id == student_id, Plan.day == day))
    ).scalar_one_or_none()
    if plan is None:
        plan = Plan(student_id=student_id, day=day)
        db.add(plan)
        await db.flush()
    plan.target_min = body.target_min
    plan.note = body.note
    plan.source = "manual"   # 家长手动改过，就不再算自动排片
    # 计划页改的目标也写回该星期的那一格，保证各处显示一致
    _set_weekday_target(student, day, body.target_min)
    student.daily_target_min = body.target_min

    await db.execute(delete(PlanItem).where(PlanItem.plan_id == plan.id))
    for index, video_id in enumerate(body.video_ids):
        db.add(PlanItem(plan_id=plan.id, video_id=video_id, order_index=index))

    await db.commit()
    return {"day": day.isoformat(), "target_min": plan.target_min, "items": len(body.video_ids)}
