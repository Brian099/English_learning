import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, Query, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import Device

_serializer = URLSafeTimedSerializer(settings.app_secret, salt="kids-listening-parent")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def issue_parent_token() -> str:
    return _serializer.dumps({"role": "parent"})


def _verify_parent_token(token: str) -> None:
    try:
        _serializer.loads(token, max_age=settings.token_days * 86400)
    except SignatureExpired as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期") from exc
    except BadSignature as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效") from exc


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    return value if scheme.lower() == "bearer" and value else None


async def require_parent(authorization: str | None = Header(None)) -> None:
    """家长端接口守卫。"""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "需要家长登录")
    _verify_parent_token(token)


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


async def read_auth(
    authorization: str | None = Header(None),
    token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """只读接口：家长令牌或设备令牌都放行。

    家长 Web 端要看内容库和转写，孩子端 App 也要看，所以这里放宽。
    写操作仍然严格区分（家长用 require_parent，上报事件用 current_device）。
    """
    raw = _bearer(authorization) or token
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少令牌")

    try:
        _verify_parent_token(raw)
        return {"role": "parent", "student_id": None}
    except HTTPException:
        pass

    device = (
        await db.execute(select(Device).where(Device.token_hash == hash_token(raw)))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效")
    device.last_seen_at = datetime.now(UTC)
    return {"role": "device", "student_id": device.student_id}
