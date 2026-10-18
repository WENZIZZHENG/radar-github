"""关注写路径（T-009，《架构决策记录》决策 9：关注＝动态入池）。

关注三态（任务书钉死，收到 repo 标识后按库内状态分流）：
1. 已入池 alive：只插 follows 行——每日 _snapshot_all 选 dead=0 全量，本就在跟踪，不打 GitHub；
2. 已入池 dead：复活（dead=0）＋立即抓一张基线快照＋插 follows——复活即回到每日跟踪选池，
   基线是死库期后增量两端的起点（discover.py 每日主流程不用改）；
3. 未入池：REST 抓单仓库元数据（GitHubClient.fetch_repo，一次请求拿全入库字段）
   → repos(source='follow')＋基线快照＋follows。

幂等与保留口径：
- 重复关注幂等：先查 follows 短路返回（不重复插、不打 GitHub）；
- 取消关注只删 follows 行，repos/snapshots 一律不动（决策 9"跟踪保留"）；
- 取消未关注/不存在的仓库幂等成功（调用方据此统一返回成功）。

大小写与规范名（direct-follow 设计决策 4）：
- 库内查找按大小写不敏感（`COLLATE NOCASE`）：`PSF/Requests` 与库内 `psf/requests` 是同一仓库，
  必须命中同一条记录，否则会被当成未入池仓库再抓一次、再插一条 repo，产生重复入池；
- 因此 `FollowOutcome.full_name` 回传的是**规范名**——命中库内记录时取库内 `full_name`，未命中时
  取 GitHub 返回的 `full_name`（fetch_repo 跟随改名 301 后即新名）。调用方（303 跳转参数、API 响应、
  结果横幅）一律用规范名，库内既有记录的 `full_name` 不被改写（不自动归并，§21.6）；
- 查库用左操作数 `COLLATE NOCASE`（语义无歧义；等价写法与实测见设计决策 4），代价是这条查询走全表扫；
  关注是人工单次操作、`repos` 6 万行级毫秒可接受，本变更不新建 NOCASE 索引（要迁移，另开任务）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from app.collector.github import GitHubClient, utc_now_iso
from app.collector.snapshot import ingest_followed_repo

# 关注结果三态（响应体 state 字段取值；仅展示/留痕用，无分支逻辑挂在取值上）
STATE_ALIVE = "alive"  # 态①：已入池活库，只补 follows
STATE_REVIVED = "revived"  # 态②：dead 复活回每日跟踪
STATE_JOINED = "joined"  # 态③：未入池动态入池


@dataclass(frozen=True)
class FollowOutcome:
    """一次关注的结果：state 为三态之一；already_followed=True 表示幂等命中（库内零变化、未打 GitHub）。"""

    repo_id: int
    full_name: str
    state: str
    already_followed: bool


async def follow_repo(
    conn: sqlite3.Connection,
    client: GitHubClient,
    full_name: str,
    *,
    now_iso: Callable[[], str] = utc_now_iso,
) -> FollowOutcome:
    """按三态处理一次关注；GitHub 404/认证/限速等异常原样上抛，由 API 层映射状态码。"""
    row = conn.execute("SELECT id, full_name, dead FROM repos WHERE full_name COLLATE NOCASE = ?", (full_name,)).fetchone()
    # 规范名口径（design 决策 4）：命中库内记录时用库内 full_name；未命中时先用输入值，态②③
    # fetch_repo 成功后再换成 GitHub 返回的 full_name。FollowOutcome.full_name 一律回传规范名，
    # 调用方（303 参数、响应体、横幅）因此不依赖用户输入的大小写变体。
    canonical = row["full_name"] if row is not None else full_name
    if row is not None:
        followed = conn.execute("SELECT 1 FROM follows WHERE repo_id = ?", (row["id"],)).fetchone() is not None
        if followed:
            # 幂等短路：已关注不重复插、不消耗 GitHub 配额（三态同口径）。
            # state 在此路径只是按 dead 标志的展示性推断：dead 仓实际并未执行复活动作（T-009 评审低-2），
            # 真语义看 already_followed=True（库内零变化）；state 仅作展示/留痕，无行为分支依赖它。
            return FollowOutcome(row["id"], canonical, STATE_ALIVE if row["dead"] == 0 else STATE_REVIVED, True)
    now = now_iso()
    if row is not None and row["dead"] == 0:
        repo_id, state = row["id"], STATE_ALIVE
    else:
        # 态②③都要最新元数据/星数：fetch_repo 一次拿全；404（不存在/转私有）上抛 → API 映射 404。
        # 打 GitHub 必须用规范名：用用户输入的大小写变体会与库内记录走两条 node_id，撞库误判（设计决策 4）。
        item = await client.fetch_repo(canonical)
        repo_id = ingest_followed_repo(conn, item, captured_at=now, now_iso=now)
        # 跟随 301 后 item["full_name"] 可能是新名（§21.6）：以 GitHub 返回值为最终规范名
        canonical = item["full_name"]
        state = STATE_REVIVED if row is not None else STATE_JOINED
    with conn:
        conn.execute("INSERT OR IGNORE INTO follows (repo_id, created_at) VALUES (?, ?)", (repo_id, now))
    return FollowOutcome(repo_id, canonical, state, False)


def unfollow_repo(conn: sqlite3.Connection, full_name: str) -> bool:
    """取消关注：只删 follows 行（决策 9 跟踪保留：repos/snapshots 不动）；返回是否真删了行。"""
    with conn:
        cur = conn.execute("DELETE FROM follows WHERE repo_id = (SELECT id FROM repos WHERE full_name = ?)", (full_name,))
    return cur.rowcount > 0
