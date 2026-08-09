import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import init_db
from app.jobs import create_scheduler, start_scheduler, stop_scheduler
from app.web.routes import STATIC_DIR
from app.web.routes import router as web_router


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
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# T-008 榜单页面路由：/ 本周报告（含 ?week= 历史周次）、/quarter 季度回顾、/total 总星榜
app.include_router(web_router)
