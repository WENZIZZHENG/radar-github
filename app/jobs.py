"""进程内调度（架构决策 7：SSR + SQLite + 进程内 APScheduler，不引独立调度进程）。

每日北京 05:00（UTC 21:00）跑一次全日任务（快照＋发现池，逻辑全在 app.collector.discover；T-011/T-017 串行接 AI 每日生成）：
- misfire_grace_time=1 小时：进程重启错过整点，1 小时内醒来补跑一次；
- coalesce=True：多次错过合并成一次，不连刷配额（共识 §8 允许数据空洞，没必要补）；
- 时区钉死 UTC：服务器本地时区不可控，采集口径全部 UTC（与 UTC 定长时间戳硬约定一致）。
T-029 手动同步（《交互流程说明》§14.4）：手动触发与调度共用同一把运行锁（全局限额一个运行实例），
状态只存进程内存不落库；web 层接口见 try_start_sync / sync_status。
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.ai import DeepSeekClient, ensure_daily_ai
from app.candidates import scan_candidates
from app.classify import load_topics
from app.collector.discover import DailyStats, get_job_logger, run_daily
from app.collector.github import GitHubClient, utc_now_iso
from app.config import BASE_DIR, get_settings
from app.db import get_conn, init_db
from app.report import precompute_boards

DAILY_JOB_ID = "daily_snapshot_discover"
MISFIRE_GRACE_SECONDS = 3600


def _candidate_scan_due(now: datetime) -> bool:
    """候选词扫描触发判定（T-032，§15.2）：仅 UTC 周一执行（与 _weekly_refill 同日判定口径，
    date.weekday() == 0）；周一以外的日子零行为变化。"""
    return now.date().weekday() == 0

# T-029 手动同步与调度共用的运行锁/状态（§14.4：全局限额一个运行实例，手动/调度共用同一把锁；
# 状态只存进程内存不落库——服务重启归零，被中断的任务由次日调度自愈）：
# 锁内原子检查＋置位（与 app.web.routes 批量任务的 in-flight 锁同口径）；S 档单进程跨请求共享，线程锁保护读写
_sync_state: dict = {
    "running": False,
    "started_at": None,  # 本轮运行开始时刻（UTC 定长）
    "last_finished_at": None,  # 上次运行结束时刻（成功/异常均更新；None = 尚无完成史，服务刚重启或从未跑过）
    "last_stats": None,  # 上次运行的 run_daily 汇总（DailyStats；异常结束保持上次值）
    "last_error": None,  # 上次运行异常域：None 正常 | "unknown" 整轮异常（细节在 data/jobs.log）
}
_sync_state_lock = threading.Lock()  # 保护 _sync_state 读写；防重入语义（running 时再触发 → 手动 409 / 调度跳过）
_sync_task: asyncio.Task | None = None  # 模块级持有后台任务引用，防 GC 意外回收


def try_start_sync() -> bool:
    """原子抢占运行锁并起后台任务（web 层手动触发与调度器共用；§14.4 全局限额一个运行实例）：
    锁内检查 running → 已在跑返回 False（调用方口径：手动 409 / 调度撞锁跳过）；否则置位
    running＋started_at、清 last_error → asyncio.create_task 起后台任务跑 daily_job 全链路
    （模块级持有防 GC）→ 返回 True。为什么锁与起任务成对放在 jobs.py：抢占与启动必须原子——
    拆开的话抢锁与 create_task 之间另一来源再触发会双跑；web 层只判返回值即可，不另造任务。
    """
    global _sync_task
    with _sync_state_lock:
        if _sync_state["running"]:
            return False
        _sync_state.update(running=True, started_at=utc_now_iso(), last_error=None)
    # create_task 失败（如无 running loop 的调用场景）必须回滚 running 置位：
    # 否则无任务去复位，running 卡 True 与 F2 同属卡死族（评审 F3 防御）
    try:
        _sync_task = asyncio.create_task(_run_sync())
    except RuntimeError:
        with _sync_state_lock:
            _sync_state.update(running=False, started_at=None)
        raise
    return True


def sync_status() -> dict:
    """web 层轮询接口（§14.2 第 2 步）：返回状态拷贝，防调用方改动内部状态；
    无任务史（服务刚重启/从未跑过）时 running=False 且各字段为 None。"""
    with _sync_state_lock:
        return dict(_sync_state)


async def daily_job() -> DailyStats | None:
    """调度入口（T-029 起先过同一把运行锁；§14.4：手动触发与调度共用，运行中任何来源再触发一律拒绝）：
    try_start_sync 抢锁——已在跑（手动触发或调度撞锁）→ 记日志跳过本轮返回 None，次日调度自然重试；
    抢到锁 → 等待后台任务完成并返回 run_daily 的 DailyStats（调度器忽略返回值；手动同步路径经
    sync_status()['last_stats'] 取用拼完成 toast）。签名/行为对调度器不变（仍是同一 job 函数、同链路）。"""
    if not try_start_sync():
        get_job_logger().info("同步已在运行（手动触发或调度撞锁）：本轮跳过，次日调度自然重试")
        return None
    return await _sync_task


async def _run_sync() -> DailyStats:
    """手动/调度共用全链路执行体（即 daily_job 的可复用体；§14.4 同步＝daily_job 全链路，不另实现一套）。

    自带连接与客户端生命周期；run_daily 内部已吞异常记日志，本层仍要兜：
    - T-011/T-017：快照/发现之后串行跑 AI 每日生成（翻译收窄范围为三口径榜∪关注集＋三维度推荐语；
      同一 conn 复用 WAL 读写不互阻；github_client 复用 run_daily 的 GitHub 连接拉 README——token 缺失时
      ensure 内部降级跳过 README 拉取、输入退化元数据、新行 readme_sha 保持 NULL；旧行指纹不被失败拉取
      清除（F2-1：拉取失败不触发重生），不报错）。
    - T-027：run_daily 成功后、AI 段之前插入榜单预计算——同一 as_of（run_daily 完成时刻，晚于本轮快照
      captured_at 使 load 判定③恒过）对三口径各 compute_boards 全量一次落 board_cache（页面打开直读）；
      整段独立 try/except 吞掉记 ERROR 不抛出——预计算失败绝不阻断 AI 段与次日调度（页面缺缓存时
      降级实时算，天然兜底）。
    - T-032：候选词扫描（§15.2）在预计算后、AI ensure 前执行——仅 UTC 周一（_candidate_scan_due 判定，
      与每周补捞同日口径），池内 topics 词频 → 未命中词表且达阈值的词 → DeepSeek 批量出建议主题 →
      全量覆盖式落库；key 缺失/调用失败内部降级跳过不清表，外层 try/except 兜底记 ERROR 不阻断 AI 段。
    - AI 整段 try/except 吞掉记 ERROR 不抛出——AI 失败永不阻断快照主流程（key 缺失在
      ensure_daily_ai 内部降级返回零统计）。
    - T-029 整函数级再兜一层：run_daily 等段内部已吞异常，能到这的多为库级/客户端构造异常——
      记 last_error（前端 toast"同步失败，稍后再试"，§14.3，细节在 jobs.log）按零统计返回不抛出，
      保证任务必然结束、锁必然释放。
    结束（成功/异常）一律置 running=False＋last_finished_at，成功回填 last_stats（调度路径同样回填，
    语义即"上次运行结果"）；状态只存进程内存不落库。
    """
    conn = None
    try:
        settings = get_settings()
        init_db()  # schema 全量 IF NOT EXISTS：调度进程可能与 uvicorn 分开发育，各自保证库表存在
        conn = get_conn()
        log = get_job_logger()
        # client 作用域必须覆盖 AI 段：曾只包住 run_daily，AI 段复用已关闭的 client 拉 README 全失败
        # （生产每日任务空转约 26 分钟、README 变更重生永不触发——T-029 预演实测抓出，生产 jobs.log 有痕）
        async with GitHubClient(settings.github_token) as client:  # token 空/无效由客户端在使用点报清晰错误
            stats = await run_daily(client, conn, log=log)
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
                    # T-032 候选词扫描（§15.2）：仅 UTC 周一、每周补捞（run_daily 内）之后执行；
                    # 扫描段内部自带 AI 降级（key 缺失/调用失败整段跳过不清表，页面显示上一轮），
                    # 外层再兜一层防意外异常——扫描失败绝不阻断 AI 生成与次日调度（§15.3 记日志不报警）
                    try:
                        if _candidate_scan_due(datetime.now(timezone.utc)):
                            cand_summary = await scan_candidates(conn, ai_client, log=log)
                            log.info("候选词扫描段完成：%s", cand_summary)
                    except Exception:
                        log.exception("候选词扫描整段异常：吞掉不抛出（页面照旧显示上一轮），下周自然重试")
                    await ensure_daily_ai(
                        conn, ai_client, now=datetime.now(timezone.utc), log=log, github_client=client
                    )
            except Exception:
                log.exception("AI 每日生成整轮异常：吞掉不抛出（AI 降级不阻断快照主流程），次日调度自然重试")
        with _sync_state_lock:
            _sync_state["last_stats"] = stats
        return stats
    except Exception:
        get_job_logger().exception("同步整轮异常：已记 last_error，返回零统计（失败细节在 jobs.log）")
        with _sync_state_lock:
            _sync_state["last_error"] = "unknown"
        return DailyStats()
    finally:
        # 状态复位先于关连接且各自独立兜底：conn.close() 抛异常不得跳过 running 复位
        # （跳过则 running 永久为 True：手动一律 409、调度每轮撞锁跳过，直到进程重启——评审 F2）
        if conn is not None:
            try:
                conn.close()
            except Exception:
                get_job_logger().exception("同步收尾 conn.close() 异常：吞掉，状态照常复位")
        with _sync_state_lock:
            _sync_state["running"] = False
            _sync_state["last_finished_at"] = utc_now_iso()


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
