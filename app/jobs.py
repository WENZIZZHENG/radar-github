"""进程内调度（架构决策 7：SSR + SQLite + 进程内 APScheduler，不引独立调度进程）。

每日 UTC 00:00 跑一次全日任务（快照＋发现池，逻辑全在 app.collector.discover）：
- misfire_grace_time=1 小时：进程重启错过整点，1 小时内醒来补跑一次；
- coalesce=True：多次错过合并成一次，不连刷配额（共识 §8 允许数据空洞，没必要补）；
- 时区钉死 UTC：服务器本地时区不可控，采集口径全部 UTC（与 UTC 定长时间戳硬约定一致）。
"""

from __future__ import annotations

from datetime import timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.collector.discover import get_job_logger, run_daily
from app.collector.github import GitHubClient
from app.config import get_settings
from app.db import get_conn, init_db

DAILY_JOB_ID = "daily_snapshot_discover"
MISFIRE_GRACE_SECONDS = 3600


async def daily_job() -> None:
    """调度入口：自带连接与客户端生命周期；run_daily 内部已吞异常记日志，调度器侧无需再兜。"""
    settings = get_settings()
    init_db()  # schema 全量 IF NOT EXISTS：调度进程可能与 uvicorn 分开发育，各自保证库表存在
    conn = get_conn()
    try:
        async with GitHubClient(settings.github_token) as client:  # token 空/无效由客户端在使用点报清晰错误
            await run_daily(client, conn, log=get_job_logger())
    finally:
        conn.close()


def create_scheduler() -> AsyncIOScheduler:
    """注册好每日 job 但未启动的调度器：注册参数离线可测；启动/停止归 main.py lifespan。"""
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    scheduler.add_job(
        daily_job,
        CronTrigger(hour=0, minute=0, timezone=timezone.utc),
        id=DAILY_JOB_ID,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        coalesce=True,
        replace_existing=True,  # 重复 create 不堆同名 job（测试多轮调用安全）
    )
    return scheduler


def start_scheduler(scheduler: AsyncIOScheduler) -> None:
    scheduler.start()


def stop_scheduler(scheduler: AsyncIOScheduler) -> None:
    # 不等跑中任务收尾：全日任务可达数百请求，服务关停不能长阻塞；任务幂等，次日调度自然重跑
    scheduler.shutdown(wait=False)
