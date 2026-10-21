from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import DailyStat, Device, PlayEvent, Video
from ..security import current_device

router = APIRouter(prefix="/api", tags=["play"])

TZ = ZoneInfo(settings.tz)


class EventIn(BaseModel):
    """客户端上报的单次播放会话。event_id 由客户端生成（建议 UUIDv7）。"""

    event_id: str = Field(min_length=8, max_length=64)
    video_id: int
    started_at: datetime
    ended_at: datetime | None = None
    effective_ms: int = 0
    max_position_ms: int = 0
    seek_count: int = 0
    avg_rate: float = 1.0
    completed: bool = False
    client_version: str | None = None


class EventBatch(BaseModel):
    events: list[EventIn] = Field(default_factory=list, max_length=500)


def day_of(moment: datetime) -> date:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(TZ).date()


async def recompute_day(db: AsyncSession, student_id: int, day: date) -> None:
    """按 (学生, 日期) 重算聚合。重算是幂等的，天然抗重复上报。

    用 ORM 聚合而不是原生 SQL，这样 SQLite 和 Postgres 行为一致。
    """
    start = datetime.combine(day, datetime.min.time(), TZ)
    end = start + timedelta(days=1)

    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(PlayEvent.effective_ms), 0),
                func.coalesce(func.sum(func.cast(PlayEvent.completed, Integer)), 0),
                func.count(PlayEvent.id),
            ).where(
                PlayEvent.student_id == student_id,
                PlayEvent.started_at >= start,
                PlayEvent.started_at < end,
            )
        )
    ).one()

    effective_ms, completed_count, session_count = int(row[0]), int(row[1]), int(row[2])

    stat = await db.get(DailyStat, (student_id, day))
    if stat is None:
        if session_count == 0:
            return
        stat = DailyStat(student_id=student_id, day=day)
        db.add(stat)
    stat.effective_ms = effective_ms
    stat.completed_count = completed_count
    stat.session_count = session_count


@router.post("/events")
async def ingest(
    body: EventBatch,
    device: Device = Depends(current_device),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """批量接收播放事件。幂等：重复 event_id 不会重复计数。"""
    if not body.events:
        return {"received": 0, "accepted": 0, "duplicate": 0, "rejected": 0}

    video_ids = {event.video_id for event in body.events}
    known_videos = set(
        (await db.execute(select(Video.id).where(Video.id.in_(video_ids)))).scalars().all()
    )

    incoming = {event.event_id: event for event in body.events}
    existing = set(
        (
            await db.execute(
                select(PlayEvent.event_id).where(PlayEvent.event_id.in_(list(incoming)))
            )
        ).scalars().all()
    )

    accepted = 0
    touched: set[tuple[int, date]] = set()
    for event_id, event in incoming.items():
        if event.video_id not in known_videos:
            continue
        if event_id in existing:
            continue
        started = event.started_at if event.started_at.tzinfo else event.started_at.replace(tzinfo=UTC)
        db.add(
            PlayEvent(
                event_id=event_id,
                student_id=device.student_id,
                device_id=device.id,
                video_id=event.video_id,
                started_at=started,
                ended_at=event.ended_at,
                effective_ms=max(0, event.effective_ms),
                max_position_ms=max(0, event.max_position_ms),
                seek_count=max(0, event.seek_count),
                avg_rate=event.avg_rate,
                completed=event.completed,
                client_version=event.client_version,
            )
        )
        accepted += 1
        touched.add((device.student_id, day_of(started)))

    await db.flush()
    for student_id, day in touched:
        await recompute_day(db, student_id, day)
    await db.commit()

    return {
        "received": len(body.events),
        "accepted": accepted,
        "duplicate": len(existing),
        "rejected": len(body.events) - accepted - len(existing),
    }
