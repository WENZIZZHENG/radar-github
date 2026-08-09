import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.db import init_db
from app.jobs import create_scheduler, start_scheduler, stop_scheduler


def jobs_enabled() -> bool:
    """RADAR_JOBS_ENABLED（默认开）：本地开发/测试置 0 关掉采集调度，避免起服务顺带打 API。"""
    get_settings()  # 触发 .env 加载（幂等）：.env 里的 RADAR_JOBS_ENABLED 同样生效，不依赖 lifespan 内调用顺序
    return os.environ.get("RADAR_JOBS_ENABLED", "1").strip().lower() not in ("0", "false", "off")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # schema 全量 IF NOT EXISTS，启动时无条件建表（T-002 评审转办，T-006 接线）
    init_db()
    scheduler = None
    if jobs_enabled():
        scheduler = create_scheduler()
        start_scheduler(scheduler)
    yield
    if scheduler is not None:
        stop_scheduler(scheduler)


app = FastAPI(title="GitHub 雷达", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """首页占位：证明服务可达；T-008 榜单页面落地后替换本页。"""
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>GitHub 雷达</title></head><body>"
        "<h1>GitHub 雷达运行中</h1>"
        "<p>榜单功能开发中（T-008 后替换本页）。</p>"
        "</body></html>"
    )
