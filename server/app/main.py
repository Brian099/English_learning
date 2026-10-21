from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .routers import auth, content, ingest, plan, play, report, student


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
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


for module in (auth, content, ingest, play, report, plan, student):
    app.include_router(module.router)

# 静态前端：必须最后挂载，否则会吃掉 /api 路由
WEB_DIR = Path(__file__).parent / "web"
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
