"""初始快照任务（T-005）：核心池（stars>=1000，约 6.4 万仓库）分片枚举 + 批量入库。

设计取舍（为什么这么做）：

- 星数区间递归二分：GitHub Search 单查询只吐前 1000 条（per_page=100 时 page<=10，验收红线）。
  先以 per_page=1 探 total_count（省流量），>1000 就对半二分、<=1000 才逐页拉取，
  从机制上保证任何分页查询都不可能越上限。
- 高星半区先跑：星数越高仓库越稀疏，枚举顺序（DFS 先高后低）的第一个叶子就是仓库最少的分片，
  `--max-shards 1` 小规模真实验证时只花约 2 次请求配额。
- 根分片星数上界取探针时刻的全站最大值：全量跑期间涨破该值的仓库本轮缺席（概率极低），
  由每日发现池（T-006）捞回，不为此换维度细分。
- REST search item 自带 full_name/description/language/topics/stargazers_count（《架构决策记录》决策 1：
  Search 仅发现/初始分片），初始枚举一次拿全，无需 GraphQL。
- 一个分片一个事务：分片内 repos + star_snapshots 两表写入同生共死；中途失败整体回滚、
  断点不落盘，重跑整片幂等重来（full_name UNIQUE + INSERT OR IGNORE 兜底）。
- 断点文件 data/snapshot_progress.json（data/ 已 gitignore）：记录已完成叶子分片。
  重跑时枚举仍逐片探 total_count（枚举本身是确定性的，探针代价约每片 1 次请求），
  但已完成分片跳过分页拉取与入库——省掉的是占大头的分页流量。
- 时间戳一律 app.collector.github.utc_now_iso()（UTC 定长硬约定）；同一分片所有快照共享一个 captured_at。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.collector.github import GitHubClient, utc_now_iso
from app.config import BASE_DIR, get_settings
from app.db import get_conn, init_db

MIN_STARS = 1000  # 核心池下界（共识文档：stars>=1000 全部公开仓库）
SEARCH_RESULT_CAP = 1000  # GitHub Search 单查询结果硬上限，验收红线
PROBE_PER_PAGE = 1  # 探 total_count 只需要 1 条：省流量
PAGE_SIZE = 100  # 逐页拉取用满每页上限：摊薄请求次数（限速 30 次/分钟）
ID_LOOKUP_CHUNK = 500  # 按 full_name 反查 id 的分块大小：防御旧编译 SQLite 的 999 变量上限，留足余量

DEFAULT_PROGRESS_PATH = BASE_DIR / "data" / "snapshot_progress.json"


class ShardSplitError(RuntimeError):
    """区间已退化为单点（min==max）但 total_count 仍越 1000 上限：星数二分法无能为力。"""


@dataclass(frozen=True)
class Shard:
    """星数区间分片；max_stars=None 仅用于根分片（核心池无星数上界）。"""

    min_stars: int
    max_stars: int | None

    @property
    def query(self) -> str:
        if self.max_stars is None:
            return f"stars:>={self.min_stars}"
        return f"stars:{self.min_stars}..{self.max_stars}"


@dataclass(frozen=True)
class LeafShard:
    """total_count<=1000、可直接逐页拉完的分片。"""

    shard: Shard
    total_count: int


@dataclass(frozen=True)
class IngestResult:
    repos_inserted: int
    snapshots_inserted: int


@dataclass
class RunStats:
    shards_seen: int = 0  # 本轮枚举到的叶子分片数（含断点跳过）：--max-shards 按它计窗口
    shards_done: int = 0
    shards_skipped: int = 0
    repos_inserted: int = 0
    snapshots_inserted: int = 0
    log_lines: list[str] = field(default_factory=list)


def split_range(min_stars: int, max_stars: int) -> tuple[Shard, Shard]:
    """对半二分，返回 (高星半, 低星半)；单点区间无法再分，直接报错而不是静默丢数据。"""
    if min_stars >= max_stars:
        raise ShardSplitError(
            f"星数区间 [{min_stars}, {max_stars}] 退化为单点但仓库数仍越 {SEARCH_RESULT_CAP} 上限："
            "星数二分法已到极限，需人工拍板改用其他维度（如 created 日期）细分后再跑"
        )
    mid = (min_stars + max_stars) // 2
    return Shard(mid + 1, max_stars), Shard(min_stars, mid)


async def iter_leaf_shards(client: GitHubClient, shard: Shard) -> AsyncIterator[LeafShard]:
    """深度优先枚举叶子分片（高星半先）：yield 顺序即处理顺序，保证 --max-shards 1 拿到最小分片。"""
    probe = await client.search_repositories(shard.query, per_page=PROBE_PER_PAGE, page=1)
    total = int(probe.get("total_count", 0))
    if total <= SEARCH_RESULT_CAP:
        yield LeafShard(shard, total)
        return
    # 越上限必须二分。根分片无上界：Search 按星数降序，探针首条即区间实际最大星数，取它当上界
    max_stars = shard.max_stars
    if max_stars is None:
        items = probe.get("items") or []
        if not items or "stargazers_count" not in items[0]:
            raise ShardSplitError(f"探针响应缺少 items[0].stargazers_count，无法为 {shard.query} 确定上界：{probe!r:.200}")
        max_stars = int(items[0]["stargazers_count"])
    high, low = split_range(shard.min_stars, max_stars)
    async for leaf in iter_leaf_shards(client, high):
        yield leaf
    async for leaf in iter_leaf_shards(client, low):
        yield leaf


async def fetch_shard_items(client: GitHubClient, leaf: LeafShard) -> list[dict]:
    """叶子分片逐页拉取：total_count<=1000 保证 page 不会越界；末页不足 100 条由 API 自然截断。"""
    pages = math.ceil(leaf.total_count / PAGE_SIZE)
    items: list[dict] = []
    for page in range(1, pages + 1):
        data = await client.search_repositories(leaf.shard.query, per_page=PAGE_SIZE, page=page)
        items.extend(data.get("items") or [])
    return items


def ingest_items(conn: sqlite3.Connection, items: list[dict], *, captured_at: str, now_iso: str) -> IngestResult:
    """search items → repos + star_snapshots，一个事务同生共死；full_name 冲突 INSERT OR IGNORE 跳过（幂等）。"""
    if not items:
        return IngestResult(0, 0)
    repo_rows = [
        (
            item["full_name"],
            item["node_id"],  # Relay 全局 ID：每日 nodes(ids:) 批量采集必需（T-006 勘误补存）
            item.get("description"),  # GitHub 官方允许为空，schema 允许 NULL
            item.get("language"),  # 同上：部分仓库无语言
            json.dumps(item.get("topics") or [], ensure_ascii=False),  # schema 硬约定：topics 存 JSON 数组字符串
            now_iso,
        )
        for item in items
    ]
    with conn:  # 一个分片一个事务：异常整体回滚，断点不落盘，重跑整片幂等
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO repos (full_name, node_id, description_en, language, topics, dead, source, created_at)
            VALUES (?, ?, ?, ?, ?, 0, 'initial', ?)
            """,
            repo_rows,
        )
        repos_inserted = cur.rowcount
        # INSERT OR IGNORE 之后拿不到可靠 lastrowid（冲突行不产生新 id）：统一按 full_name 反查，分块防变量上限
        id_by_name: dict[str, int] = {}
        names = [row[0] for row in repo_rows]
        for offset in range(0, len(names), ID_LOOKUP_CHUNK):
            chunk = names[offset : offset + ID_LOOKUP_CHUNK]
            placeholders = ", ".join("?" * len(chunk))
            rows = conn.execute(f"SELECT id, full_name FROM repos WHERE full_name IN ({placeholders})", chunk)
            for row in rows:
                id_by_name[row["full_name"]] = row["id"]
        snapshot_rows = [(id_by_name[item["full_name"]], captured_at, item["stargazers_count"]) for item in items]
        cur = conn.executemany(
            "INSERT OR IGNORE INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            snapshot_rows,
        )
        snapshots_inserted = cur.rowcount
    return IngestResult(repos_inserted, snapshots_inserted)


def ingest_followed_repo(conn: sqlite3.Connection, item: dict, *, captured_at: str, now_iso: str) -> int:
    """单仓库关注入池/复活（《架构决策记录》决策 9 动态入池）：repos(source='follow')＋基线快照一个事务同生共死。

    - 未入池：插入 repos 行（source='follow'，字段映射与 ingest_items 同口径）＋当行基线快照；
    - 已入池 dead（复活）：repos 行 INSERT OR IGNORE 跳过（元数据不动），仅置 dead=0 回到每日
      _snapshot_all 的 dead=0 选池，并补一张最新基线快照（死库期间星数无采集，增量两端从这里起算）；
    - 幂等：full_name/node_id 撞已有行 INSERT OR IGNORE 跳过，基线快照主键 (repo_id, captured_at) 冲突跳过；
    - 返回 repo_id（新插或既有行），供调用方接着写 follows；
    - fail loud：full_name 不在库但 INSERT 被 IGNORE（node_id 撞已有行＝仓库改名旧名在库）时抛 ValueError，
      事务整体回滚不留半写；改名归并超出当前边界（T-006 评审中-1 同型坑，既定姿态）；
    - fail loud：full_name 在库但 node_id 与 GitHub 现值不一致（原仓库被删除后同名重建）时抛 ValueError
      （T-009 评审中-1：不拦则会带着旧 node_id 复活，次日 nodes(ids:) 拿 null 被静默再标 dead）。
    """
    with conn:  # repos＋快照要么都成要么都败：异常整体回滚，重试幂等
        conn.execute(
            """
            INSERT OR IGNORE INTO repos (full_name, node_id, description_en, language, topics, dead, source, created_at)
            VALUES (?, ?, ?, ?, ?, 0, 'follow', ?)
            """,
            (
                item["full_name"],
                item["node_id"],  # Relay 全局 ID：次日 nodes(ids:) 批量采集入口，必填
                item.get("description"),  # GitHub 官方允许为空，schema 允许 NULL
                item.get("language"),  # 同上
                json.dumps(item.get("topics") or [], ensure_ascii=False),  # schema 硬约定：JSON 数组字符串
                now_iso,
            ),
        )
        row = conn.execute("SELECT id, dead, node_id FROM repos WHERE full_name = ?", (item["full_name"],)).fetchone()
        if row is None:
            raise ValueError(
                f"仓库 {item['full_name']} 的 node_id 与库内已有记录冲突（仓库可能已改名，旧名仍在库），暂未自动归并"
            )
        if row["node_id"] != item["node_id"]:
            # 同名重建（T-009 评审中-1）：与改名撞库同姿态 fail loud，由本人知情处置，不自动归并
            raise ValueError(f"仓库 {item['full_name']} 的 node_id 已变化（原仓库可能被删除后同名重建），暂未自动归并")
        repo_id = row["id"]
        if row["dead"]:
            conn.execute("UPDATE repos SET dead = 0 WHERE id = ?", (repo_id,))
        conn.execute(
            "INSERT OR IGNORE INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, captured_at, item["stargazers_count"]),
        )
    return repo_id


class ProgressStore:
    """叶子分片断点（JSON）：已完成分片重跑跳过；损坏时 fail loud 由人工处置（删掉=全量重跑，入库幂等不丢数据）。

    格式：{"version": 1, "updated_at": ..., "completed": {"stars:A..B": {total_count, repos_inserted,
    snapshots_inserted, finished_at}}}；写入走临时文件 + replace 原子替换，中断不会留下半个 JSON。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.completed: dict[str, dict] = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"断点文件 {self.path} 不是有效 JSON：请人工检查后修复或删除（删除即全量重跑，入库幂等）"
                ) from exc
            self.completed = dict(data.get("completed") or {})

    def is_done(self, shard_key: str) -> bool:
        return shard_key in self.completed

    def mark_done(
        self,
        shard_key: str,
        *,
        total_count: int,
        repos_inserted: int,
        snapshots_inserted: int,
        finished_at: str,
    ) -> None:
        """分片入库 commit 成功后立即落盘：先写临时文件再原子替换。"""
        self.completed[shard_key] = {
            "total_count": total_count,
            "repos_inserted": repos_inserted,
            "snapshots_inserted": snapshots_inserted,
            "finished_at": finished_at,
        }
        payload = {"version": 1, "updated_at": finished_at, "completed": self.completed}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.path)  # os.replace：同文件系统内原子，Windows 下可覆盖已存在目标


async def run_snapshot(
    client: GitHubClient,
    conn: sqlite3.Connection,
    progress: ProgressStore,
    *,
    max_shards: int | None = None,
    now_iso: Callable[[], str] = utc_now_iso,
    log: Callable[[str], None] = print,
) -> RunStats:
    """枚举核心池叶子分片并逐片入库。

    max_shards 限定枚举顺序上的前 N 个叶子分片（含断点已完成的）：同一命令重跑幂等——
    已完成的跳过、不继续向后推进，库内行数不变（全量 6.4 万是部署期动作，平时验证用 1）。
    """
    stats = RunStats()

    def emit(message: str) -> None:
        stats.log_lines.append(message)
        log(message)

    async for leaf in iter_leaf_shards(client, Shard(MIN_STARS, None)):
        if max_shards is not None and stats.shards_seen >= max_shards:
            emit(f"已达 --max-shards={max_shards}（枚举顺序前 {max_shards} 个叶子分片），本轮停止（剩余分片留待下次运行）")
            break
        stats.shards_seen += 1
        shard_key = leaf.shard.query
        if progress.is_done(shard_key):
            stats.shards_skipped += 1
            emit(f"[跳过] {shard_key} total_count={leaf.total_count}（断点已完成，不重拉不入库）")
            continue
        # 同一分片 repos.created_at 与 star_snapshots.captured_at 共享一个时间戳：UTC 定长硬约定 + 同批一致
        now = now_iso()
        items = await fetch_shard_items(client, leaf)
        drift = abs(len(items) - leaf.total_count)
        if drift > max(10, leaf.total_count // 10):
            # 星数实时漂移是常态（探针与拉取之间仓库移入/移出区间）：显著偏差留痕告警，不修数据，
            # 漏抓的仓库由每日发现池（T-006）补捞
            emit(
                f"[告警] {shard_key} 探针 total_count={leaf.total_count} 与实际拉取 {len(items)} 偏差 {drift}："
                "分片间漂移，漏抓仓库由每日发现池补捞"
            )
        result = ingest_items(conn, items, captured_at=now, now_iso=now)
        progress.mark_done(
            shard_key,
            total_count=leaf.total_count,
            repos_inserted=result.repos_inserted,
            snapshots_inserted=result.snapshots_inserted,
            finished_at=now,
        )
        stats.shards_done += 1
        stats.repos_inserted += result.repos_inserted
        stats.snapshots_inserted += result.snapshots_inserted
        emit(
            f"[完成] {shard_key} total_count={leaf.total_count} 拉取 {len(items)} 条；"
            f"入库 repos +{result.repos_inserted}，snapshots +{result.snapshots_inserted}"
        )
    emit(
        f"本轮汇总：枚举 {stats.shards_seen} 片，新完成 {stats.shards_done} 片，跳过 {stats.shards_skipped} 片；"
        f"repos +{stats.repos_inserted}，snapshots +{stats.snapshots_inserted}"
    )
    return stats


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.collector.snapshot",
        description="初始快照任务：核心池（stars>=1000）分片枚举 + 批量入库（断点续跑，幂等）",
    )
    parser.add_argument(
        "--max-shards",
        type=int,
        default=None,
        metavar="N",
        help="只处理枚举顺序上的前 N 个叶子分片（含断点已完成的，重跑同一命令幂等；默认不限=全量；小规模真实验证用 1）",
    )
    return parser.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> None:
    settings = get_settings()
    # schema 全量 IF NOT EXISTS，重复执行安全；应用启动接线归 T-006，CLI 自己保证库表存在
    init_db()
    conn = get_conn()
    try:
        progress = ProgressStore(DEFAULT_PROGRESS_PATH)
        async with GitHubClient(settings.github_token) as client:  # token 空/无效由客户端在使用点报清晰错误
            await run_snapshot(client, conn, progress, max_shards=args.max_shards)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    asyncio.run(_async_main(_parse_args(argv)))


if __name__ == "__main__":
    main()
