import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import Account, Device, Family, PairCode, Student
from ..security import (
    current_account,
    hash_password,
    hash_token,
    issue_account_token,
    new_token,
    require_member,
    require_owner,
    verify_password,
)

router = APIRouter(prefix="/api", tags=["auth"])


def _as_utc(moment: datetime) -> datetime:
    """SQLite 读回来的 datetime 不带时区，比较前统一补成 UTC。"""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class LoginBody(BaseModel):
    username: str = "parent"
    password: str


class PairCodeBody(BaseModel):
    student_id: int
    device_name: str = Field(default="平板")


class PairBody(BaseModel):
    code: str
    device_name: str = "平板"
    platform: str | None = None


class AccountBody(BaseModel):
    username: str
    password: str
    display_name: str | None = None


class AccountPatch(BaseModel):
    display_name: str | None = None
    password: str | None = None
    active: bool | None = None


class FamilyBody(BaseModel):
    name: str


# ------------------------------------------------------------------ 登录

@router.post("/admin/login")
async def login(body: LoginBody, db: AsyncSession = Depends(get_db)) -> dict:
    """账号登录：用户名 + 密码。"""
    account = (
        await db.execute(select(Account).where(Account.username == body.username.strip()))
    ).scalar_one_or_none()
    if account is None or not account.active or not verify_password(
        body.password, account.password_hash
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")

    family = await db.get(Family, account.family_id)
    return {
        "token": issue_account_token(account),
        "expires_in_days": settings.token_days,
        "role": account.role,
        "username": account.username,
        "family": family.name if family else "",
    }


@router.get("/account/me")
async def me(account: Account = Depends(current_account), db: AsyncSession = Depends(get_db)) -> dict:
    family = await db.get(Family, account.family_id)
    return {
        "account_id": account.id,
        "username": account.username,
        "display_name": account.display_name,
        "role": account.role,
        "family_id": account.family_id,
        "family": family.name if family else "",
    }


@router.patch("/family", dependencies=[Depends(require_owner)])
async def rename_family(
    body: FamilyBody, account: Account = Depends(require_owner), db: AsyncSession = Depends(get_db)
) -> dict:
    family = await db.get(Family, account.family_id)
    if family is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "家庭不存在")
    family.name = body.name.strip() or family.name
    await db.commit()
    return {"id": family.id, "name": family.name}


# ------------------------------------------------------------------ 子账号

@router.get("/accounts", dependencies=[Depends(require_owner)])
async def list_accounts(
    account: Account = Depends(require_owner), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    rows = (
        await db.execute(
            select(Account).where(Account.family_id == account.family_id).order_by(Account.id)
        )
    ).scalars().all()
    return [
        {
            "id": a.id,
            "username": a.username,
            "display_name": a.display_name,
            "role": a.role,
            "active": a.active,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


@router.post("/accounts", dependencies=[Depends(require_owner)])
async def create_account(
    body: AccountBody,
    account: Account = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """主账号建子账号（role=member），用来管学生和计划。"""
    username = body.username.strip()
    if len(username) < 3:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名至少 3 个字符")
    if len(body.password) < 6:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码至少 6 位")
    exists = (
        await db.execute(select(Account).where(Account.username == username))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已被占用")

    created = Account(
        family_id=account.family_id,
        username=username,
        password_hash=hash_password(body.password),
        role="member",
        display_name=body.display_name,
    )
    db.add(created)
    await db.commit()
    await db.refresh(created)
    return {"id": created.id, "username": created.username, "role": created.role}


@router.patch("/accounts/{account_id}", dependencies=[Depends(require_owner)])
async def patch_account(
    account_id: int,
    body: AccountPatch,
    account: Account = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    target = await db.get(Account, account_id)
    if target is None or target.family_id != account.family_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
    if body.display_name is not None:
        target.display_name = body.display_name
    if body.password:
        if len(body.password) < 6:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "密码至少 6 位")
        target.password_hash = hash_password(body.password)
    if body.active is not None:
        if target.id == account.id and not body.active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能停用自己")
        target.active = body.active
    await db.commit()
    return {"id": target.id, "active": target.active}


@router.delete("/accounts/{account_id}", dependencies=[Depends(require_owner)])
async def delete_account(
    account_id: int,
    account: Account = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    target = await db.get(Account, account_id)
    if target is None or target.family_id != account.family_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
    if target.role == "owner":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除主账号")
    await db.delete(target)
    await db.commit()
    return {"deleted": account_id}


# ------------------------------------------------------------------ 设备配对

@router.post("/devices/pair-code", dependencies=[Depends(require_member)])
async def create_pair_code(
    body: PairCodeBody,
    account: Account = Depends(require_member),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """子账号生成 6 位配对码，孩子在设备上输入一次即可换长期令牌。"""
    student = await db.get(Student, body.student_id)
    if student is None or student.family_id != account.family_id:
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


@router.get("/devices", dependencies=[Depends(require_member)])
async def list_devices(
    account: Account = Depends(require_member), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    rows = (
        await db.execute(
            select(Device)
            .join(Student, Student.id == Device.student_id)
            .where(Student.family_id == account.family_id)
            .order_by(Device.created_at.desc())
        )
    ).scalars().all()
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


@router.delete("/devices/{device_id}", dependencies=[Depends(require_member)])
async def revoke_device(
    device_id: int,
    account: Account = Depends(require_member),
    db: AsyncSession = Depends(get_db),
) -> dict:
    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    student = await db.get(Student, device.student_id)
    if student is None or student.family_id != account.family_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "设备不存在")
    await db.delete(device)
    await db.commit()
    return {"revoked": device_id}
