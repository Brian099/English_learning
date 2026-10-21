from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Plan, PlanItem, Student, Video
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

    await db.execute(delete(PlanItem).where(PlanItem.plan_id == plan.id))
    for index, video_id in enumerate(body.video_ids):
        db.add(PlanItem(plan_id=plan.id, video_id=video_id, order_index=index))

    await db.commit()
    return {"day": day.isoformat(), "target_min": plan.target_min, "items": len(body.video_ids)}
