"""每日任务（T-006）：全池星数快照 + 发现池捞新面孔 + 死库标记，供调度器与 CLI dry-run 共用。

设计取舍（为什么这么做）：

- 快照走 GraphQL nodes(ids:) 分批（≤100/批）：核心池全量约 6.4 万仓库≈640 请求（架构决策 1），
  比逐仓 REST 省一个数量级；批内 null / isPrivate / isDisabled 是死库信号 → repos.dead=1，
  只停采不删历史快照（schema 注释口径）。
- 整轮共享一个 captured_at（任务启动时刻 utc_now_iso()）：与 T-005 同批一致口径，
  "当日快照"后续按 <= 截止日取最近一行的榜单 SQL 不受影响。
- 发现池每日只捞 stars:>=1000 按 updated 降序前 5 页（500 条）：星数榜头部常年固化，
  按最近活跃排序才能轮到涨星中的新仓库；判重按 full_name＋node_id 双键（改名库不算新面孔，
  含死库——存在即不动），当行写基线快照，使新入池首周缺席口径（决策 4）有据可依。
- 静默容错（共识 §8：连续 3 天失败允许数据空洞，次日调度自然重试）：
  单批/单页失败记日志后继续跑完本批之外的量；整任务异常吞掉只记日志，不报警、不抛出。
- 任务日志双写 data/jobs.log（RotatingFileHandler，data/ 已 gitignore）与控制台，
  每轮结束记一行汇总（快照/新发现/dead/耗时），出问题有据可查。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.collector.github import MAX_NODES_PER_QUERY, GitHubAuthError, GitHubClient, utc_now_iso
from app.collector.snapshot import ID_LOOKUP_CHUNK
from app.config import BASE_DIR, get_settings
from app.db import get_conn, init_db

DISCOVER_QUERY = "stars:>=1000"  # 发现池查询：与核心池同下界，按 updated 排序捞新面孔
DISCOVER_PAGES = 5  # 每日只看前 500 条活跃仓库：再深的页新面孔密度极低，不值得配额
DEFAULT_LOG_PATH = BASE_DIR / "data" / "jobs.log"

logger = logging.getLogger("radar.jobs")


@dataclass
class DailyStats:
    snapshots_written: int = 0  # 本轮写入的当日快照行数
    discovered: int = 0  # 本轮新入池仓库数（source='discover'）
    dead_marked: int = 0  # 本轮新标记死库数


def get_job_logger(log_path: str | Path = DEFAULT_LOG_PATH) -> logging.Logger:
    """任务日志器：滚动文件 + 控制台双写；重复调用不叠加 handler（pytest 同进程多轮跑安全）。"""
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 不传给 root：避免 pytest 等环境已配 root handler 时双写
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)  # data/ 不入仓，首次运行补建
    file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def _apply_snapshot_batch(
    conn: sqlite3.Connection,
    chunk: list[sqlite3.Row],
    nodes: list[dict | None],
    captured_at: str,
    stats: DailyStats,
) -> None:
    """一批 nodes 落库：活库写当日快照（INSERT OR REPLACE，同秒重跑幂等覆盖），死库置 dead=1 停采。"""
    with conn:  # 一批一个事务：半批失败整体回滚，重跑整批幂等
        for row, node in zip(chunk, nodes):
            if node is None or node.get("isPrivate") or node.get("isDisabled"):
                conn.execute("UPDATE repos SET dead = 1 WHERE id = ?", (row["id"],))
                stats.dead_marked += 1
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
                    (row["id"], captured_at, node["stargazerCount"]),
                )
                stats.snapshots_written += 1


async def _snapshot_all(
    client: GitHubClient, conn: sqlite3.Connection, *, captured_at: str, stats: DailyStats, log: logging.Logger
) -> None:
    """每日快照：全部 dead=0 仓库按 node_id 分批走 nodes(ids:)；单批失败记日志跳过，不拖垮整轮。"""
    rows = conn.execute("SELECT id, node_id FROM repos WHERE dead = 0").fetchall()
    for offset in range(0, len(rows), MAX_NODES_PER_QUERY):
        chunk = rows[offset : offset + MAX_NODES_PER_QUERY]
        try:
            nodes = await client.fetch_repos_by_ids([row["node_id"] for row in chunk])
            _apply_snapshot_batch(conn, chunk, nodes, captured_at, stats)
        except GitHubAuthError:
            raise  # token 失效是确定性错误：逐批重试只会刷爆日志还可能触发二次限速，直通整轮 handler 记一次
        except Exception:
            log.exception("快照批次失败（%d 个仓库，自第 %d 个起）：跳过本批继续，缺口由次日调度补", len(chunk), offset)


def _ingest_discovered(conn: sqlite3.Connection, items: list[dict], *, now_iso: str, captured_at: str) -> int:
    """新面孔入池（source='discover'）＋当行基线快照；映射口径与 snapshot.ingest_items 对齐，返回实插行数。"""
    if not items:
        return 0
    repo_rows = [
        (
            item["full_name"],
            item["node_id"],  # Relay 全局 ID：次日 nodes(ids:) 批量采集的入口，必填
            item.get("description"),  # GitHub 官方允许为空，schema 允许 NULL
            item.get("language"),  # 同上
            json.dumps(item.get("topics") or [], ensure_ascii=False),  # schema 硬约定：topics 存 JSON 数组字符串
            now_iso,
        )
        for item in items
    ]
    with conn:  # 入池与基线快照同生共死：失败整体回滚，次日发现池重捞
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO repos (full_name, node_id, description_en, language, topics, dead, source, created_at)
            VALUES (?, ?, ?, ?, ?, 0, 'discover', ?)
            """,
            repo_rows,
        )
        inserted = cur.rowcount
        # 与 T-005 同口径：INSERT OR IGNORE 后按 full_name 反查 id，分块防旧 SQLite 变量上限
        id_by_name: dict[str, int] = {}
        names = [row[0] for row in repo_rows]
        for offset in range(0, len(names), ID_LOOKUP_CHUNK):
            part = names[offset : offset + ID_LOOKUP_CHUNK]
            placeholders = ", ".join("?" * len(part))
            for row in conn.execute(f"SELECT id, full_name FROM repos WHERE full_name IN ({placeholders})", part):
                id_by_name[row["full_name"]] = row["id"]
        snapshot_rows = []
        for item in items:
            rid = id_by_name.get(item["full_name"])
            if rid is None:
                # 该行被 INSERT OR IGNORE 跳过（node_id 撞已有行，如改名仓库）：不配套写基线——
                # 反查落空直接下标会抛 KeyError 把整批入池拖成回滚（T-006 评审中-1 教训）
                continue
            snapshot_rows.append((rid, captured_at, item["stargazers_count"]))
        conn.executemany(
            "INSERT OR IGNORE INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            snapshot_rows,
        )
    return inserted


async def _discover(
    client: GitHubClient, conn: sqlite3.Connection, *, captured_at: str, stats: DailyStats, log: logging.Logger
) -> None:
    """发现池：按 updated 降序捞前 500 条活跃仓库，新面孔入池＋基线快照；单页失败记日志继续下一页。"""
    # 判重按 full_name + node_id 双键：仓库改名后 full_name 变、node_id 不变，
    # 只按名字判会把改名库当"新面孔"——INSERT 撞 node_id UNIQUE 被 IGNORE，后续反查落空（评审中-1）
    seen_names = {row["full_name"] for row in conn.execute("SELECT full_name FROM repos")}
    seen_ids = {row["node_id"] for row in conn.execute("SELECT node_id FROM repos")}
    new_items: list[dict] = []
    for page in range(1, DISCOVER_PAGES + 1):
        try:
            data = await client.search_repositories(DISCOVER_QUERY, sort="updated", per_page=100, page=page)
        except GitHubAuthError:
            raise  # token 失效是确定性错误：逐页重试只会刷爆日志，直通整轮 handler 记一次
        except Exception:
            log.exception("发现池第 %d 页拉取失败：跳过本页继续，缺口由次日调度补", page)
            continue
        for item in data.get("items") or []:
            name = item.get("full_name")
            nid = item.get("node_id")
            if not name or name in seen_names or nid in seen_ids:
                continue
            seen_names.add(name)  # 页间去重：同一仓库可能因 updated 排序抖动出现在多页
            seen_ids.add(nid)
            new_items.append(item)
    stats.discovered += _ingest_discovered(conn, new_items, now_iso=captured_at, captured_at=captured_at)


async def run_daily(
    client: GitHubClient,
    conn: sqlite3.Connection,
    *,
    now_iso: Callable[[], str] = utc_now_iso,
    log: logging.Logger | None = None,
) -> DailyStats:
    """执行一次全日任务（快照→发现池）；任何异常吞掉记日志不抛出（共识 §8），汇总行必定落日志。"""
    log = log or get_job_logger()
    started = time.monotonic()
    captured_at = now_iso()  # 整轮共享一个时间戳：UTC 定长硬约定 + 同批一致
    stats = DailyStats()
    try:
        await _snapshot_all(client, conn, captured_at=captured_at, stats=stats, log=log)
        await _discover(client, conn, captured_at=captured_at, stats=stats, log=log)
    except Exception:
        log.exception("每日任务整轮异常：吞掉不报警（共识 §8 允许数据空洞），次日调度自然重试")
    elapsed = time.monotonic() - started
    log.info(
        "每日任务汇总：快照 %d 行、新发现 %d 个、dead 标记 %d 个、耗时 %.1f 秒（captured_at=%s）",
        stats.snapshots_written,
        stats.discovered,
        stats.dead_marked,
        elapsed,
        captured_at,
    )
    return stats


async def _async_main() -> None:
    settings = get_settings()
    init_db()  # schema 全量 IF NOT EXISTS，重复执行安全；CLI 不经 main.py，自己保证库表存在
    conn = get_conn()
    try:
        log = get_job_logger()
        async with GitHubClient(settings.github_token) as client:  # token 空/无效由客户端在使用点报清晰错误
            await run_daily(client, conn, log=log)
    finally:
        conn.close()


def main() -> None:
    """CLI dry-run 入口：`python -m app.collector.discover` 直接跑一次全日任务（不经调度器）。"""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
