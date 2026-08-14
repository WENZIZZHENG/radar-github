"""进程内调度（架构决策 7：SSR + SQLite + 进程内 APScheduler，不引独立调度进程）。

每日北京 05:00（UTC 21:00）跑一次全日任务（快照＋发现池，逻辑全在 app.collector.discover；T-011/T-017 串行接 AI 每日生成）：
- misfire_grace_time=1 小时：进程重启错过整点，1 小时内醒来补跑一次；
- coalesce=True：多次错过合并成一次，不连刷配额（共识 §8 允许数据空洞，没必要补）；
- 时区钉死 UTC：服务器本地时区不可控，采集口径全部 UTC（与 UTC 定长时间戳硬约定一致）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.ai import DeepSeekClient, ensure_daily_ai
from app.classify import load_topics
from app.collector.discover import get_job_logger, run_daily
from app.collector.github import GitHubClient, utc_now_iso
from app.config import BASE_DIR, get_settings
from app.db import get_conn, init_db
from app.report import precompute_boards

DAILY_JOB_ID = "daily_snapshot_discover"
MISFIRE_GRACE_SECONDS = 3600


async def daily_job() -> None:
    """调度入口：自带连接与客户端生命周期；run_daily 内部已吞异常记日志，调度器侧无需再兜。

    T-011/T-017：快照/发现之后串行跑 AI 每日生成（翻译收窄范围为三口径榜∪关注集＋三维度推荐语；
    同一 conn 复用 WAL 读写不互阻；github_client 复用 run_daily 的 GitHub 连接拉 README——token 缺失时
    ensure 内部降级跳过 README 拉取、输入退化元数据、新行 readme_sha 保持 NULL；旧行指纹不被失败拉取
    清除（F2-1：拉取失败不触发重生），不报错）。
    T-027：run_daily 成功后、AI 段之前插入榜单预计算——同一 as_of（run_daily 完成时刻，晚于本轮快照
    captured_at 使 load 判定③恒过）对三口径各 compute_boards 全量一次落 board_cache（页面打开直读）；
    整段独立 try/except 吞掉记 ERROR 不抛出——预计算失败绝不阻断 AI 段与次日调度（页面缺缓存时
    降级实时算，天然兜底）。
    AI 整段 try/except 吞掉记 ERROR 不抛出——AI 失败永不阻断快照主流程（key 缺失在
    ensure_daily_ai 内部降级返回零统计，此路径行为与接线前一致）。
    """
    settings = get_settings()
    init_db()  # schema 全量 IF NOT EXISTS：调度进程可能与 uvicorn 分开发育，各自保证库表存在
    conn = get_conn()
    try:
        log = get_job_logger()
        async with GitHubClient(settings.github_token) as client:  # token 空/无效由客户端在使用点报清晰错误
            await run_daily(client, conn, log=log)
        try:
            # T-027 榜单预计算：as_of 取 run_daily 完成时刻（>= 本轮快照 captured_at，同日不误判过期）
            table = load_topics(BASE_DIR / "config" / "topics.yaml")
            as_of = utc_now_iso()
            summary = precompute_boards(conn, table, as_of=as_of)
            log.info("榜单预计算完成：as_of=%s，各口径榜数/主榜行数/新区行数=%s", as_of, summary)
        except Exception:
            log.exception("榜单预计算异常：吞掉不抛出（页面缺缓存时降级实时算兜底），次日调度自然重试")
        try:
            async with DeepSeekClient(settings.deepseek_api_key) as ai_client:
                await ensure_daily_ai(
                    conn, ai_client, now=datetime.now(timezone.utc), log=log, github_client=client
                )
        except Exception:
            log.exception("AI 每日生成整轮异常：吞掉不抛出（AI 降级不阻断快照主流程），次日调度自然重试")
    finally:
        conn.close()


def create_scheduler() -> AsyncIOScheduler:
    """注册好每日 job 但未启动的调度器：注册参数离线可测；启动/停止归 main.py lifespan。"""
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    scheduler.add_job(
        daily_job,
        CronTrigger(hour=21, minute=0, timezone=timezone.utc),  # UTC 21:00 = 北京次日 05:00（2026-08-14 本人拍板改定）
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
