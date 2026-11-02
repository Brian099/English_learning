import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .routers import auth, content, ingest, maintenance, overview, plan, play, report, student

logger = logging.getLogger("kids")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    if settings.parent_password == "kids1234":
        logger.warning(
            "家长密码仍是默认值 kids1234 —— 部署后请在 compose 的 PARENT_PASSWORD 里改掉"
        )
    yield


app = FastAPI(title="Kids English Listening API", version="0.3.0", lifespan=lifespan)

# 前端与 API 同源部署时不需要 CORS；跨域调试时可临时放开
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "version": "0.3.0",
        "media_root": settings.media_root,
        "tz": settings.tz,
        "database": settings.database_url.split("://", 1)[0],
    }


@app.exception_handler(Exception)
async def unhandled_exception(_request: Request, exc: Exception) -> JSONResponse:
    """兜底：未处理异常也返回 JSON。

    默认的纯文本 "Internal Server Error" 会让前端 JSON.parse 报
    "unexpected token I"，把真正的错误盖住。
    """
    logger.exception("未处理异常")
    text = str(exc) or type(exc).__name__
    if "database is locked" in text.lower():
        text = "数据库正忙（后台正在导入/处理），请稍后重试"
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {text[:200]}"},
    )


for module in (auth, content, ingest, play, report, plan, student, maintenance, overview):
    app.include_router(module.router)

# 静态前端：必须最后挂载，否则会吃掉 /api 路由
WEB_DIR = Path(__file__).parent / "web"
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
