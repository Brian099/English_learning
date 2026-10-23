from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Plan, PlanItem, Student, Video
from ..scheduler import build_daily_plan
from ..security import read_auth, require_parent

router = APIRouter(prefix="/api", tags=["plan"])


class PlanBody(BaseModel):
    target_min: int = Field(default=20, ge=0, le=180)
    note: str | None = None
    video_ids: list[int] = Field(default_factory=list)


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
    """自动排片：复习到期 + 难度匹配 + 时长装填，覆盖该天计划。

    家长之后仍可以在页面上手动调整（手动保存会把 source 改回 manual）。
    """
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
            "message": "没有可排的内容（视频库为空，或当天都已完成）",
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
    # 家长显式指定了目标，就同步成默认目标，避免"设置页还是旧数字"
    if target_min:
        student.daily_target_min = built["target_min"]
    plan.note = (f"自动排片：复习 {built['review_count']} · 新内容 {built['new_count']}"
                 f" · 重复 {built['repeat_count']}")

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
    # 计划页改的目标也写回默认目标，保证设置页、概览页、学生端显示一致
    student.daily_target_min = body.target_min

    await db.execute(delete(PlanItem).where(PlanItem.plan_id == plan.id))
    for index, video_id in enumerate(body.video_ids):
        db.add(PlanItem(plan_id=plan.id, video_id=video_id, order_index=index))

    await db.commit()
    return {"day": day.isoformat(), "target_min": plan.target_min, "items": len(body.video_ids)}
