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
    row = conn.execute("SELECT id, dead FROM repos WHERE full_name = ?", (full_name,)).fetchone()
    if row is not None:
        followed = conn.execute("SELECT 1 FROM follows WHERE repo_id = ?", (row["id"],)).fetchone() is not None
        if followed:
            # 幂等短路：已关注不重复插、不消耗 GitHub 配额（三态同口径）。
            # state 在此路径只是按 dead 标志的展示性推断：dead 仓实际并未执行复活动作（T-009 评审低-2），
            # 真语义看 already_followed=True（库内零变化）；state 仅作展示/留痕，无行为分支依赖它。
            return FollowOutcome(row["id"], full_name, STATE_ALIVE if row["dead"] == 0 else STATE_REVIVED, True)
    now = now_iso()
    if row is not None and row["dead"] == 0:
        repo_id, state = row["id"], STATE_ALIVE
    else:
        # 态②③都要最新元数据/星数：fetch_repo 一次拿全；404（不存在/转私有）上抛 → API 映射 404
        item = await client.fetch_repo(full_name)
        repo_id = ingest_followed_repo(conn, item, captured_at=now, now_iso=now)
        state = STATE_REVIVED if row is not None else STATE_JOINED
    with conn:
        conn.execute("INSERT OR IGNORE INTO follows (repo_id, created_at) VALUES (?, ?)", (repo_id, now))
    return FollowOutcome(repo_id, full_name, state, False)


def unfollow_repo(conn: sqlite3.Connection, full_name: str) -> bool:
    """取消关注：只删 follows 行（决策 9 跟踪保留：repos/snapshots 不动）；返回是否真删了行。"""
    with conn:
        cur = conn.execute("DELETE FROM follows WHERE repo_id = (SELECT id FROM repos WHERE full_name = ?)", (full_name,))
    return cur.rowcount > 0
