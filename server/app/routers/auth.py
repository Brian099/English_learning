import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Device, PairCode, Student
from ..security import hash_token, issue_parent_token, new_token, require_parent

router = APIRouter(prefix="/api", tags=["auth"])


def _as_utc(moment: datetime) -> datetime:
    """SQLite 读回来的 datetime 不带时区，比较前统一补成 UTC。

    Postgres 的 timestamptz 本来就是带时区的，这里对它是无损的。
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class LoginBody(BaseModel):
    password: str


class PairCodeBody(BaseModel):
    student_id: int
    device_name: str = Field(default="平板")


class PairBody(BaseModel):
    code: str
    device_name: str = "平板"
    platform: str | None = None


@router.post("/admin/login")
async def login(body: LoginBody) -> dict:
    """家长登录。密码来自环境变量 PARENT_PASSWORD。"""
    if not secrets.compare_digest(body.password, settings.parent_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "密码错误")
    return {"token": issue_parent_token(), "expires_in_days": settings.token_days}


@router.post("/devices/pair-code", dependencies=[Depends(require_parent)])
async def create_pair_code(body: PairCodeBody, db: AsyncSession = Depends(get_db)) -> dict:
    """家长端生成 6 位配对码，孩子在 App 里输入一次即可换长期令牌。"""
    student = await db.get(Student, body.student_id)
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "学生不存在")

    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = datetime.now(UTC) + timedelta(seconds=settings.pair_code_ttl)
    db.add(PairCode(code=code, student_id=student.id, expires_at=expires))
    await db.commit()
    return {"code": code, "expires_at": expires.isoformat(), "student": student.name}


class PairResult(BaseModel):
    token: str
    student_id: int
    student_name: str
    device_id: int


@router.post("/devices/pair", response_model=PairResult)
async def pair(body: PairBody, db: AsyncSession = Depends(get_db)) -> PairResult:
    row = await db.get(PairCode, body.code)
    if row is None or row.used_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "配对码无效")
    if _as_utc(row.expires_at) < datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "配对码已过期")

    student = await db.get(Student, row.student_id)
    if student is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "学生不存在")

    token = new_token()
    device = Device(
        student_id=student.id,
        name=body.device_name,
        token_hash=hash_token(token),
        platform=body.platform,
    )
    db.add(device)
    row.used_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(device)

    return PairResult(
        token=token, student_id=student.id, student_name=student.name, device_id=device.id
    )


@router.get("/devices", dependencies=[Depends(require_parent)])
async def list_devices(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Device).order_by(Device.created_at.desc()))).scalars().all()
    return [
        {
            "id": d.id,
            "student_id": d.student_id,
            "name": d.name,
            "platform": d.platform,
            "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
        }
        for d in rows
    ]


@router.delete("/devices/{device_id}", dependencies=[Depends(require_parent)])
async def revoke_device(device_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    await db.delete(device)
    await db.commit()
    return {"revoked": device_id}
