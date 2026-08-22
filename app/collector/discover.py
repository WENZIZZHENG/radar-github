"""每日任务（T-006）：全池星数快照 + 发现池捞新面孔 + 死库标记，供调度器与 CLI dry-run 共用。

设计取舍（为什么这么做）：

- 快照走 GraphQL nodes(ids:) 分批（≤100/批）：核心池全量约 6.4 万仓库≈640 请求（架构决策 1），
  比逐仓 REST 省一个数量级；批内 null / isPrivate / isDisabled 是死库信号 → repos.dead=1，
  只停采不删历史快照（schema 注释口径）；node_id 在 GitHub 侧不可解析（errors 报 "Could not resolve
  to a node"，仓已删除/转移）与上述信号同类 → 坏仓标 dead=1 剔除后重试本批（每轮至少剔一个，循环有界），
  提不出坏 id 才按单批失败跳过。
- T-016/T-017 变更检测（决策 5 v2）：nodes 本已拉 description 字段，与库内 description_en 比对，
  有变更 → 更新原文＋清 description_zh=NULL＋DELETE 该仓全部维度推荐语（当日 ensure 对范围内仓自然重译/重生）；
  无变更不动。T-025 追加 language/topics 漂移检测（流程说明 §7.4）：只 UPDATE repos 归类字段，
  不清译文不删推荐语——归类读时实时算，UPDATE 后次日榜单自然生效（漂移率低、重生成本高）。
- 整轮共享一个 captured_at（任务启动时刻 utc_now_iso()）：与 T-005 同批一致口径，
  "当日快照"后续按 <= 截止日取最近一行的榜单 SQL 不受影响。
- 发现池每日只捞 stars:>=1000 按 updated 降序前 5 页（500 条）：星数榜头部常年固化，
  按最近活跃排序才能轮到涨星中的新仓库；判重按 full_name＋node_id 双键（改名库不算新面孔，
  含死库——存在即不动），当行写基线快照，使新入池首周缺席口径（决策 4）有据可依。
- 发现池每日另加一条"新仓定向"查询（stars:>=1000 created:>=45 天前，sort=stars，与每日同配额）：
  补 GitHub 搜索索引视图不一致盲区——索引最终一致性会让刚爆炸的新仓在"星数门槛+按 updated 排序"
  视图缺席、却在"按星数排序"视图可见（deepseek-harness 实测：updated 视图连捞两轮缺席；
  星数排序视图里 ≥64000 波段排第 132、带 created 限定的本查询 total 161 排第 1）；
  两条查询互为冗余，先 ingest 每日结果、定向查询从库重读 seen 自然判重。
- T-019 每周补捞（仅 UTC 周一触发，嵌入 run_daily 每日发现之后）：星数区间 7 段指数划分
  （1000..2000 … >=64000）按 ISO 周序号轮换、sort=stars 捞前 500 条——补 updated 排序盲区：
  老仓慢涨过 1000 星但近期无更新永远翻不进每日前 500；7 周扫完全谱，长尾段 7 周轮转、
  头部优先（每段只捞 Top 500 属既定截断）；判重与入池完全复用发现池路径。
- 静默容错（共识 §8：连续 3 天失败允许数据空洞，次日调度自然重试）：
  快照批内坏仓（node_id 不可解析）剔除重试、提不出坏 id 才记日志跳过本批，单页失败记日志后继续下一页；
  整任务异常吞掉只记日志，不报警、不抛出。
- 任务日志双写 data/jobs.log（RotatingFileHandler，data/ 已 gitignore）与控制台，
  每轮结束记一行汇总（快照/新发现/dead/漂移/耗时），出问题有据可查。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.collector.github import (
    MAX_NODES_PER_QUERY,
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    normalize_github_created_at,
    utc_now_iso,
)
from app.collector.snapshot import ID_LOOKUP_CHUNK
from app.config import BASE_DIR, get_settings
from app.db import get_conn, init_db

DISCOVER_QUERY = "stars:>=1000"  # 发现池查询：与核心池同下界，按 updated 排序捞新面孔
DISCOVER_PAGES = 5  # 每日只看前 500 条活跃仓库：再深的页新面孔密度极低，不值得配额
# 新仓定向窗口：45 天（约一个半月）内创建、且已过 1000 星门槛的仓库。GitHub 搜索索引最终一致性会让
# 刚爆炸的新仓（如 deepseek-harness 两天 9.6 万星）在"星数门槛+按 updated 排序"视图缺席，却在
# 按星数排序视图可见；45 天≈一个半月，兼顾"新仓"语义（更窄会漏掉涨得稍慢的爆款，更宽则退化回全谱）。
DISCOVER_NEW_WINDOW_DAYS = 45
# T-019 每周补捞：星数区间 7 段（指数划分），仅 UTC 周一按 ISO 周序号轮换，7 周扫完全谱
DISCOVER_BANDS = [
    (1000, 2000),
    (2000, 4000),
    (4000, 8000),
    (8000, 16000),
    (16000, 32000),
    (32000, 64000),
    (64000, None),  # 开放段：stars:>=64000（GitHub Search 区间语法到上限即用 >=）
]
DISCOVER_BAND_PAGES = 5  # 每波段捞前 500 条（stars 降序头部优先），与每日同配额档
DEFAULT_LOG_PATH = BASE_DIR / "data" / "jobs.log"

logger = logging.getLogger("radar.jobs")


@dataclass
class DailyStats:
    snapshots_written: int = 0  # 本轮写入的当日快照行数
    discovered: int = 0  # 本轮新入池仓库数（source='discover'）
    dead_marked: int = 0  # 本轮新标记死库数
    drift_updated: int = 0  # 本轮 language/topics 漂移更新归类字段的仓库数（T-025；同仓双漂移也只 +1）


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
    """一批 nodes 落库：活库写当日快照（INSERT OR REPLACE，同秒重跑幂等覆盖），死库置 dead=1 停采。

    T-016/T-017 变更检测：nodes 返回的 description 与库内 description_en 比对，有变更 → 更新原文并清旧译文
    （description_zh=NULL）＋DELETE 该仓全部维度推荐语（recommendations，可再生数据；当日 ensure 对范围内仓
    重生自愈，与译文清除同一检测点）；无变更不动。
    T-025 追加 language/topics 漂移检测（与 description 检测并列独立）：任一漂移 → 只 UPDATE repos 归类字段
    （language/topics 一并写 nodes 侧新值，保持简单），不清 description_zh、不 DELETE recommendations——
    推荐语/译文输入是简介与 README，归类是读时实时算的，UPDATE 后次日榜单自然生效；漂移率低、重生成本高。
    T-033 追加 GitHub 创建时间顺手回填（NODES_QUERY 本已返回 createdAt）：归一化为 schema 定长后写入
    github_created_at；值有变才写（首次回填后恒等，不刷行）；createdAt 缺失/畸形 → 跳过更新保留既有值
    （展示字段，不因它中断采集主链路）。
    topics 按集合比较（防 GitHub 返回顺序抖动造成伪漂移），集合相等不写库（避免顺序扰动刷行）、有差异才
    按 GitHub 返回原序 json.dumps(ensure_ascii=False) 写回；row['topics'] 脏数据 json.loads 抛错属 fail-loud
    不捕获（与 report.py 惯例一致）；node 结构缺键按 KeyError/TypeError 上抛（协议字段，缺了就是真异常）。
    """
    with conn:  # 一批一个事务：半批失败整体回滚，重跑整批幂等
        for row, node in zip(chunk, nodes):
            if node is None or node.get("isPrivate") or node.get("isDisabled"):
                conn.execute("UPDATE repos SET dead = 1 WHERE id = ?", (row["id"],))
                stats.dead_marked += 1
            else:
                desc = node.get("description")  # GitHub 官方允许为空：None 与库内 None 相等视为无变更
                if desc != row["description_en"]:
                    conn.execute(
                        "UPDATE repos SET description_en = ?, description_zh = NULL WHERE id = ?",
                        (desc, row["id"]),
                    )
                    # T-017：简介变更 → 当日清该仓全部维度推荐语（可再生数据；当日 ensure 范围内重生自愈）
                    conn.execute("DELETE FROM recommendations WHERE repo_id = ?", (row["id"],))
                # T-033：GitHub 创建时间顺手回填（与 T-016/T-025 检测并列独立）——createdAt 缺失/畸形
                # 返回 None → 跳过更新（保留既有值，防把已回填值覆盖回 NULL）
                created_at = normalize_github_created_at(node.get("createdAt"))
                if created_at is not None and created_at != row["github_created_at"]:
                    conn.execute("UPDATE repos SET github_created_at = ? WHERE id = ?", (created_at, row["id"]))
                # T-025：language/topics 漂移检测——任一漂移只更新归类字段（不清译文不删推荐语，见 docstring）；
                # topics 按集合比较防顺序抖动伪漂移，写入按 GitHub 返回原序
                lang_node = node.get("primaryLanguage")  # GitHub 官方允许为空：None 与库内 None 相等视为无变更
                language = lang_node["name"] if lang_node is not None else None
                topics = [t["topic"]["name"] for t in node["repositoryTopics"]["nodes"]]
                if language != row["language"] or set(topics) != set(json.loads(row["topics"])):
                    conn.execute(
                        "UPDATE repos SET language = ?, topics = ? WHERE id = ?",
                        (language, json.dumps(topics, ensure_ascii=False), row["id"]),
                    )
                    stats.drift_updated += 1
                conn.execute(
                    "INSERT OR REPLACE INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
                    (row["id"], captured_at, node["stargazerCount"]),
                )
                stats.snapshots_written += 1


# GraphQL errors 里"node 不可解析"消息的 node_id 提取正则（仓已删除/转移：与 null/isPrivate/isDisabled 同类死库信号）
_UNRESOLVABLE_NODE_RE = re.compile(r"global id of '([^']+)'")


def _extract_unresolvable_node_ids(exc: GitHubError) -> set[str]:
    """从 GitHubError 消息里提取不可解析的 node_id（errors 数组 join 后可能含多条，全部提取）。"""
    return set(_UNRESOLVABLE_NODE_RE.findall(str(exc)))


async def _snapshot_chunk(
    client: GitHubClient,
    conn: sqlite3.Connection,
    chunk: list[sqlite3.Row],
    *,
    captured_at: str,
    stats: DailyStats,
    log: logging.Logger,
) -> None:
    """单批快照＋坏仓剔除重试：GitHubError 消息里提取出的不可解析 node_id（仓已删除/转移）标 dead=1 后
    剔除重试本批，循环直到成功或提不出新坏 id（每轮至少剔一个，循环自然有界）；
    提不出坏 id（或坏 id 不在本批）原样上抛，归调用方按单批失败跳过。
    """
    alive = chunk
    while True:
        try:
            nodes = await client.fetch_repos_by_ids([row["node_id"] for row in alive])
            _apply_snapshot_batch(conn, alive, nodes, captured_at, stats)
            return
        except GitHubAuthError:
            raise  # token 失效是确定性错误：直通，不进坏仓剔除（与 _snapshot_all 直通分支同口径）
        except GitHubError as exc:
            dead_ids = _extract_unresolvable_node_ids(exc) & {row["node_id"] for row in alive}
            if not dead_ids:
                raise  # 提不出坏 id 或坏 id 不在本批：上抛，按既有单批失败跳过处理
            with conn:  # 与 _apply_snapshot_batch 同事务风格：一批一个事务
                conn.executemany("UPDATE repos SET dead = 1 WHERE node_id = ?", [(nid,) for nid in sorted(dead_ids)])
            stats.dead_marked += len(dead_ids)
            log.warning(
                "快照批次剔除 %d 个不可解析 node_id（仓已删除/转移，标 dead=1 停采）：%s（%s）",
                len(dead_ids),
                ", ".join(sorted(dead_ids)),
                exc,
            )
            alive = [row for row in alive if row["node_id"] not in dead_ids]


async def _snapshot_all(
    client: GitHubClient, conn: sqlite3.Connection, *, captured_at: str, stats: DailyStats, log: logging.Logger
) -> None:
    """每日快照：全部 dead=0 仓库按 node_id 分批走 nodes(ids:)；批内坏仓剔除重试（_snapshot_chunk），
    提不出坏 id 才记日志跳过本批，不拖垮整轮。

    SELECT 带 description_en/language/topics 供 T-016/T-025 变更检测（_apply_snapshot_batch 内比对更新），
    github_created_at 供 T-033 顺手回填比对（值有变才写）。
    """
    rows = conn.execute(
        "SELECT id, node_id, description_en, language, topics, github_created_at FROM repos WHERE dead = 0"
    ).fetchall()
    for offset in range(0, len(rows), MAX_NODES_PER_QUERY):
        chunk = rows[offset : offset + MAX_NODES_PER_QUERY]
        try:
            await _snapshot_chunk(client, conn, chunk, captured_at=captured_at, stats=stats, log=log)
        except GitHubAuthError:
            raise  # token 失效是确定性错误：逐批重试只会刷爆日志还可能触发二次限速，直通整轮 handler 记一次
        except Exception:
            log.exception("快照批次失败（%d 个仓库，自第 %d 个起）：跳过本批继续，缺口由次日调度补", len(chunk), offset)


def _ingest_discovered(conn: sqlite3.Connection, items: list[dict], *, now_iso: str, captured_at: str) -> int:
    """新面孔入池（source='discover'）＋当行基线快照；映射口径与 snapshot.ingest_items 对齐，返回实插行数。

    T-033：Search item 的 created_at 一并归一化存入 github_created_at（缺失/畸形 → NULL，次日 GraphQL
    回填兜底，采集不因展示字段中断）。
    """
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
            normalize_github_created_at(item.get("created_at")),  # T-033：REST Search item 自带创建时间
        )
        for item in items
    ]
    with conn:  # 入池与基线快照同生共死：失败整体回滚，次日发现池重捞
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO repos
                (full_name, node_id, description_en, language, topics, dead, source, created_at, github_created_at)
            VALUES (?, ?, ?, ?, ?, 0, 'discover', ?, ?)
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


async def _collect_new_faces(
    client: GitHubClient,
    conn: sqlite3.Connection,
    *,
    query: str,
    sort: str,
    pages: int,
    label: str,
    log: logging.Logger,
) -> list[dict]:
    """翻页捞＋双键判重收集新面孔（T-019 起每日发现与每周补捞共用），不落库，判重/入池归调用方。

    判重按 full_name + node_id 双键：仓库改名后 full_name 变、node_id 不变，
    只按名字判会把改名库当"新面孔"——INSERT 撞 node_id UNIQUE 被 IGNORE，后续反查落空（评审中-1）。
    seen 每次调用从库重读：补捞嵌在每日发现之后执行，同轮刚入池的仓自然覆盖。
    单页失败记日志继续下一页（与 _snapshot_all 同容错）；token 失效直通整轮 handler 记一次。
    """
    seen_names = {row["full_name"] for row in conn.execute("SELECT full_name FROM repos")}
    seen_ids = {row["node_id"] for row in conn.execute("SELECT node_id FROM repos")}
    new_items: list[dict] = []
    for page in range(1, pages + 1):
        try:
            data = await client.search_repositories(query, sort=sort, per_page=100, page=page)
        except GitHubAuthError:
            raise  # token 失效是确定性错误：逐页重试只会刷爆日志，直通整轮 handler 记一次
        except Exception:
            log.exception("%s第 %d 页拉取失败：跳过本页继续，缺口由下次调度补", label, page)
            continue
        for item in data.get("items") or []:
            name = item.get("full_name")
            nid = item.get("node_id")
            if not name or name in seen_names or nid in seen_ids:
                continue
            seen_names.add(name)  # 页间去重：同一仓库可能因排序抖动出现在多页
            seen_ids.add(nid)
            new_items.append(item)
    return new_items


async def _discover(
    client: GitHubClient, conn: sqlite3.Connection, *, captured_at: str, stats: DailyStats, log: logging.Logger
) -> None:
    """发现池：updated 视图捞前 500 条活跃仓库＋新仓定向（stars 视图，created 45 天窗口）互为冗余，
    新面孔入池＋基线快照；单页失败记日志继续下一页。

    定向查询先 ingest 每日结果再执行：_collect_new_faces 每次从库重读 seen，同仓双命中只入池一次。
    """
    new_items = await _collect_new_faces(
        client, conn, query=DISCOVER_QUERY, sort="updated", pages=DISCOVER_PAGES, label="发现池", log=log
    )
    stats.discovered += _ingest_discovered(conn, new_items, now_iso=captured_at, captured_at=captured_at)
    # 新仓定向：GitHub 搜索索引最终一致性会让爆炸式新仓缺席 updated 视图但可见于 stars 视图（docstring 口径）
    cutoff = date.fromisoformat(captured_at[:10]) - timedelta(days=DISCOVER_NEW_WINDOW_DAYS)
    directed_query = f"{DISCOVER_QUERY} created:>={cutoff.isoformat()}"
    directed_items = await _collect_new_faces(
        client, conn, query=directed_query, sort="stars", pages=DISCOVER_PAGES, label="新仓定向", log=log
    )
    inserted = _ingest_discovered(conn, directed_items, now_iso=captured_at, captured_at=captured_at)
    stats.discovered += inserted
    # 独立日志行（仿每周补捞）：事后回溯"某仓是哪条查询捞进来的"时有直接证据
    log.info("新仓定向执行：查询 %s、新面孔 %d 个、新入池 %d 个", directed_query, len(directed_items), inserted)


async def _weekly_refill(
    client: GitHubClient, conn: sqlite3.Connection, *, captured_at: str, stats: DailyStats, log: logging.Logger
) -> None:
    """T-019 每周补捞：仅 UTC 周一触发，星数区间按 ISO 周序号轮换（7 周扫完全谱）、
    sort=stars 捞前 500 条——补 updated 排序盲区（老仓慢涨过 1000 星但近期无更新）；判重/入池复用发现池路径。
    已知限度（评审 F3-2 留痕）：ISO 跨年周数跳变（W53→W1）处个别波段间隔拉长或短期重复，无状态方案固有限度。"""
    day = date.fromisoformat(captured_at[:10])
    if day.weekday() != 0:
        return
    low, high = DISCOVER_BANDS[day.isocalendar().week % len(DISCOVER_BANDS)]
    query = f"stars:>={low}" if high is None else f"stars:{low}..{high}"
    new_items = await _collect_new_faces(
        client, conn, query=query, sort="stars", pages=DISCOVER_BAND_PAGES, label="每周补捞", log=log
    )
    inserted = _ingest_discovered(conn, new_items, now_iso=captured_at, captured_at=captured_at)
    stats.discovered += inserted
    log.info("每周补捞执行：波段 %s、新面孔 %d 个、新入池 %d 个", query, len(new_items), inserted)


async def run_daily(
    client: GitHubClient,
    conn: sqlite3.Connection,
    *,
    now_iso: Callable[[], str] = utc_now_iso,
    log: logging.Logger | None = None,
) -> DailyStats:
    """执行一次全日任务（快照→发现池→每周补捞（T-019 仅周一））；任何异常吞掉记日志不抛出（共识 §8），汇总行必定落日志。"""
    log = log or get_job_logger()
    started = time.monotonic()
    captured_at = now_iso()  # 整轮共享一个时间戳：UTC 定长硬约定 + 同批一致
    stats = DailyStats()
    try:
        await _snapshot_all(client, conn, captured_at=captured_at, stats=stats, log=log)
        await _discover(client, conn, captured_at=captured_at, stats=stats, log=log)
        await _weekly_refill(client, conn, captured_at=captured_at, stats=stats, log=log)  # T-019：仅 UTC 周一补捞
    except Exception:
        log.exception("每日任务整轮异常：吞掉不报警（共识 §8 允许数据空洞），次日调度自然重试")
    elapsed = time.monotonic() - started
    log.info(
        "每日任务汇总：快照 %d 行、新发现 %d 个、dead 标记 %d 个、漂移更新 %d 个、耗时 %.1f 秒（captured_at=%s）",
        stats.snapshots_written,
        stats.discovered,
        stats.dead_marked,
        stats.drift_updated,
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
