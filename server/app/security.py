import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, Query, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import Account, Device, Student

_serializer = URLSafeTimedSerializer(settings.app_secret, salt="kids-listening-parent")

PBKDF2_ROUNDS = 120_000


# ------------------------------------------------------------------ 密码

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS
    ).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$")
    except (AttributeError, ValueError):
        return False
    if algo != "pbkdf2":
        return False
    calc = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS
    ).hex()
    return secrets.compare_digest(calc, digest)


# ------------------------------------------------------------------ 令牌

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def issue_account_token(account: Account) -> str:
    return _serializer.dumps(
        {
            "kind": "account",
            "account_id": account.id,
            "family_id": account.family_id,
            "role": account.role,
        }
    )


def _load_account_payload(token: str) -> dict:
    try:
        payload = _serializer.loads(token, max_age=settings.token_days * 86400)
    except SignatureExpired as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期") from exc
    except BadSignature as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效") from exc
    if payload.get("kind") != "account":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效")
    return payload


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    return value if scheme.lower() == "bearer" and value else None


# ------------------------------------------------------------------ 守卫

async def current_account(
    authorization: str | None = Header(None), db: AsyncSession = Depends(get_db)
) -> Account:
    """登录账号守卫。"""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "需要登录")
    payload = _load_account_payload(token)
    account = await db.get(Account, payload["account_id"])
    if account is None or not account.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在或已停用")
    return account


async def require_owner(account: Account = Depends(current_account)) -> Account:
    """主账号：家庭、子账号、内容维护。"""
    if account.role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要主账号权限")
    return account


async def require_member(account: Account = Depends(current_account)) -> Account:
    """子账号：学生与计划管理。主账号不能代劳。"""
    if account.role != "member":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要子账号权限")
    return account


async def current_device(
    authorization: str | None = Header(None),
    token: str | None = Query(None, description="播放器无法设置请求头时用查询参数传"),
    db: AsyncSession = Depends(get_db),
) -> Device:
    """孩子端设备守卫：Bearer 头优先，其次 ?token=。"""
    raw = _bearer(authorization) or token
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少设备令牌")
    device = (
        await db.execute(select(Device).where(Device.token_hash == hash_token(raw)))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "设备未配对或令牌已撤销")
    device.last_seen_at = datetime.now(UTC)
    return device


async def device_family_id(db: AsyncSession, device: Device) -> int | None:
    student = await db.get(Student, device.student_id)
    return student.family_id if student else None


async def read_auth(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """只读接口：账号令牌或设备令牌都放行。

    返回 {kind, role, account_id?, family_id, student_id?}，
    调用方据此做家庭隔离。
    """
    raw = _bearer(authorization) or token
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少令牌")

    try:
        payload = _load_account_payload(raw)
        account = await db.get(Account, payload["account_id"])
        if account is not None and account.active:
            return {
                "kind": "account",
                "role": account.role,
                "account_id": account.id,
                "family_id": account.family_id,
                "student_id": None,
            }
    except HTTPException:
        pass

    device = (
        await db.execute(select(Device).where(Device.token_hash == hash_token(raw)))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效")
    device.last_seen_at = datetime.now(UTC)
    student = await db.get(Student, device.student_id)
    return {
        "kind": "device",
        "role": "device",
        "account_id": None,
        "family_id": student.family_id if student else None,
        "student_id": device.student_id,
    }
