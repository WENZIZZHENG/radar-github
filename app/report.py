"""榜单计算：周/季增量榜与总星榜，全部本地 SQL 实时计算、不物化（《架构决策记录》决策 3/4）。

口径（决策 4 逐字落实，勿自由发挥）：
- 周/季增量 = 两端快照差（端点星数 − 名义窗口天数前端点星数）；
- 端点缺失时滑动取实际可得两端：周接受 5~9 天、季接受 86~94 天跨度，结果标注实际窗口天数；
- 新入池首周缺席增量榜：不满最小窗口跨度（含只有一个快照）一律缺席，绝不拿单日增量混排；
- 负增量正常参与排序（降序下自然沉底）；dead=1 仓库全部剔除；
- 总星榜 = as_of 之前最新快照星数降序，无增量、无窗口概念。

SQL 红线（schema.sql 硬约定，T-002 评审 EXPLAIN 实测整表扫为 2300 万行级）：
端点快照查询必须从 repos 驱动、per-repo `WHERE repo_id=? AND captured_at<=? ORDER BY captured_at DESC LIMIT 1`
走主键 (repo_id, captured_at) 索引 seek；禁止对 star_snapshots 整表 GROUP BY 或相关子查询全扫。
本模块逐 alive 仓库做 ≤3 次 LIMIT 1 索引 seek（end、名义起点前最近、名义起点后最早），
6.4 万 repo × 每口径 ≤3 次 seek 可接受，换取执行计划恒定无全表扫。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.classify import LANGUAGES, OTHER_LANGUAGE_KEY, TopicSpec, classify_language, classify_topics
from app.collector.github import utc_now_iso

# 主题零命中仓库的归组 key：classify 层刻意不发明该 key（空列表语义留给调用方），榜单层在此统一定义
OTHER_TOPIC_KEY = "other"

# 增量口径参数：(名义窗口天数, 滑动窗口下限, 滑动窗口上限)。
# 周 5~9 天为决策 4 钉死；季 86~94 天＝90±4 是实施自定参数（冻结物只钉"季榜 90 天同口径"），
# 容差随窗口量级同比放宽（季窗口约为周的 13 倍）——已在 T-007 详情卡留痕，后续页面标注以此为准
_INCREMENT_WINDOWS: dict[str, tuple[int, int, int]] = {
    "week": (7, 5, 9),
    "quarter": (90, 86, 94),
}
PERIODS: tuple[str, ...] = ("week", "quarter", "total")

ABSENT_FIRST_WEEK = "first_week"  # 缺席原因：不满最小窗口跨度（含只有一个快照），口径上统称"首周缺席"

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # schema 硬约定：UTC 定长，字典序即时间序

# 端点快照 SQL 单独提为常量：__main__ 自检与测试可用 EXPLAIN QUERY PLAN 核对同一条语句的索引 seek 性质
_ENDPOINT_SQL = (
    "SELECT captured_at, stars FROM star_snapshots "
    "WHERE repo_id = ? AND captured_at <= ? "
    "ORDER BY captured_at DESC LIMIT 1"
)

# 滑动窗口的起点右邻候选：名义起点之后、end 之前最早一张。
# 决策 4"取最近可用快照"的最近可能落在名义起点任一侧（采集空洞跨过起点日时在右侧），两侧都取才不会误缺席
_START_AFTER_SQL = (
    "SELECT captured_at, stars FROM star_snapshots "
    "WHERE repo_id = ? AND captured_at > ? AND captured_at < ? "
    "ORDER BY captured_at ASC LIMIT 1"
)


@dataclass(frozen=True)
class RepoDelta:
    """单仓库单口径的增量计算结果（compute_repo_deltas 的返回值元素）。

    缺席与 0 增量必须可分：决策 4 首周缺席不是 0，页面关注区对缺席项目要显示"——"而非 0，
    故 delta/window_days 用 None 表达缺席、absent_reason 给机器可判的原因。
    """

    stars: int  # 端点（最近）快照星数，即页面"总星数"展示值
    delta: int | None  # 两端星数差，允许为负；None = 缺席；总星榜口径恒 None
    window_days: float | None  # 实际两端跨度（天，保留 1 位小数）；决策 4 要求报告标注；None = 缺席/总星榜
    absent_reason: str | None  # ABSENT_FIRST_WEEK 或 None（出席）；总星榜无缺席概念恒 None


@dataclass(frozen=True)
class ReportRow:
    """榜单行：T-008 页面层每行需要的最小字段集（frozen，可安全跨榜共享同一实例）。"""

    full_name: str
    description_en: str | None
    language: str | None  # GitHub 原始语言名（可能 None），页面展示用；归组 key 在 Board 上
    stars: int  # 总星数（端点快照星数）
    delta: int | None  # 增量；总星榜恒 None
    window_days: float | None  # 实际窗口跨度天数；总星榜恒 None；首周缺席项目不出现在榜行中


@dataclass(frozen=True)
class Board:
    """一张榜：kind+key 是机器标识（页面锚点/路由用），label 是展示名，rows 已按口径排序并截 Top N。"""

    kind: str  # "language" | "topic"
    key: str  # 语言 key（LANGUAGES 值/other）或主题 key（词表 key/other）
    label: str  # 展示名：语言用 GitHub 精确名（"Java"…），主题用词表 label，两个兜底榜为"其它语言"/"其他"
    rows: list[ReportRow]


def _parse_iso(ts: str) -> datetime:
    """按 schema 定长硬约定解析；带偏移或缺秒的变体直接报错，不静默容错（比较语义全靠定长）。"""
    return datetime.strptime(ts, _ISO_FORMAT).replace(tzinfo=timezone.utc)


def _endpoint_snapshot(conn: sqlite3.Connection, repo_id: int, before: str) -> sqlite3.Row | None:
    """取单仓库在 before（含）之前最近一张快照：主键 (repo_id, captured_at) 索引 seek，每仓库一次 LIMIT 1。"""
    return conn.execute(_ENDPOINT_SQL, (repo_id, before)).fetchone()


# 指定 repo_ids 过滤时按 full_name 反查的分块大小：防御旧编译 SQLite 的 999 变量上限（与 snapshot 同口径）
_REPO_FILTER_CHUNK = 500


def compute_repo_deltas(
    conn: sqlite3.Connection, *, period: str, as_of: str | None = None, repo_ids: Collection[int] | None = None
) -> dict[int, RepoDelta]:
    """逐 alive 仓库计算指定口径的增量结果，返回 {repo_id: RepoDelta}。

    - period="week"/"quarter"：两端快照差 + 滑动窗口校验，缺席记 absent_reason=ABSENT_FIRST_WEEK；
    - period="total"：只取最新端点星数，无增量（delta/window_days 恒 None）；
    - as_of 缺省取当前 UTC；指定历史 as_of 即回看历史周次（P2 页面的切换依据）；
    - repo_ids 缺省 None＝全池 alive（既有行为）；传入集合则只算这些仓库——"我的关注"区只复算关注集
      （T-008 评审中-2 转办，T-009 落地）：6.4 万仓全池逐仓端点查询是秒级/页浪费；
      dead 剔除口径不变：传入集合中的 dead 仓库同样不进结果（调用方按缺席口径自行补端点星数）；
    - as_of 之前无任何快照的仓库不在返回 dict 中（无总星数可展示，任何榜都安放不了）。

    公开给页面层："我的关注"区需要逐仓库判断首周缺席（显示"——"）而非混入 0 增量。
    """
    if period not in PERIODS:
        raise ValueError(f"未知榜单口径：{period!r}，可选 {PERIODS}")
    as_of = as_of or utc_now_iso()
    as_of_dt = _parse_iso(as_of)  # 顺带校验调用方给的 as_of 符合定长硬约定

    # dead=0 过滤下推 SQL：死库连端点查询都不必做（决策 4 剔除榜单）
    if repo_ids is None:
        ids = [row["id"] for row in conn.execute("SELECT id FROM repos WHERE dead = 0")]
    else:
        ids = []
        requested = list(repo_ids)
        for offset in range(0, len(requested), _REPO_FILTER_CHUNK):
            chunk = requested[offset : offset + _REPO_FILTER_CHUNK]
            placeholders = ", ".join("?" * len(chunk))
            rows = conn.execute(f"SELECT id FROM repos WHERE dead = 0 AND id IN ({placeholders})", chunk)
            ids.extend(row["id"] for row in rows)
    result: dict[int, RepoDelta] = {}
    for repo_id in ids:
        end = _endpoint_snapshot(conn, repo_id, as_of)
        if end is None:
            continue
        if period == "total":
            result[repo_id] = RepoDelta(stars=end["stars"], delta=None, window_days=None, absent_reason=None)
        else:
            result[repo_id] = _increment_delta(conn, repo_id, end, as_of_dt, period)
    return result


def _increment_delta(
    conn: sqlite3.Connection, repo_id: int, end: sqlite3.Row, as_of_dt: datetime, period: str
) -> RepoDelta:
    """单仓库周/季增量结果：起点取名义起点两侧最近候选，跨度校验通过才算出席。

    起点候选取名义起点两侧最近各一张：S1 起点当日或之前最近，S2 起点之后、end 之前最早。
    只取 S1 会把"起点日空洞、最近快照在右侧"的仓库误判缺席（如唯一起点候选在 T-5d）；
    S2 之右的候选跨度只会更小、S1 之左只会更大，故各取一张即可覆盖全部可能。
    两侧候选都在可接受窗口时取离名义起点更近的一张（"取最近可用快照"的直译，等距时 S1 先入优先）。
    """
    nominal, win_min, win_max = _INCREMENT_WINDOWS[period]
    nominal_start_dt = as_of_dt - timedelta(days=nominal)
    nominal_start = nominal_start_dt.strftime(_ISO_FORMAT)
    candidates: list[sqlite3.Row] = []
    s1 = _endpoint_snapshot(conn, repo_id, nominal_start)
    if s1 is not None:
        candidates.append(s1)
    s2 = conn.execute(_START_AFTER_SQL, (repo_id, nominal_start, end["captured_at"])).fetchone()
    if s2 is not None:
        candidates.append(s2)
    end_dt = _parse_iso(end["captured_at"])
    best: tuple[float, sqlite3.Row, float] | None = None  # (距名义起点秒数, 起点快照, 跨度天数)
    for cand in candidates:
        cand_dt = _parse_iso(cand["captured_at"])
        span_days = (end_dt - cand_dt).total_seconds() / 86400
        if not win_min <= span_days <= win_max:
            continue  # 滑出可接受区间（含 start=end 同一张 span=0）：两端代表性不足
        distance = abs((cand_dt - nominal_start_dt).total_seconds())
        if best is None or distance < best[0]:
            best = (distance, cand, span_days)
    if best is None:
        # 两侧候选都滑出窗口或根本无起点候选：典型即新入池项目，历史无法回填，按口径缺席
        return RepoDelta(stars=end["stars"], delta=None, window_days=None, absent_reason=ABSENT_FIRST_WEEK)
    _, start, span_days = best
    return RepoDelta(
        stars=end["stars"],
        delta=end["stars"] - start["stars"],
        window_days=round(span_days, 1),
        absent_reason=None,
    )


def _top(rows: list[ReportRow], period: str, top_n: int) -> list[ReportRow]:
    """按口径排序并截取：增量/总星降序为第一键，负增量自然沉底；星数、全名作次序键保证结果可复现。"""
    if period == "total":
        ordered = sorted(rows, key=lambda r: (-r.stars, r.full_name))
    else:
        ordered = sorted(rows, key=lambda r: (-(r.delta or 0), -r.stars, r.full_name))
    return ordered[:top_n]


def compute_boards(
    conn: sqlite3.Connection,
    topic_table: dict[str, TopicSpec],
    *,
    period: str = "week",
    as_of: str | None = None,
    top_n: int = 30,
) -> list[Board]:
    """算指定口径的全部分类榜：语言榜 7 张 + 主题榜（词表主题数 + 1）张，每榜 Top top_n 行。

    - topic_table 由调用方 load_topics() 后传入（与 classify.py 同口径：词表路径决策留在调用方）；
    - 当前封板词表 9 主题 → 共 17 张榜（决策 3）；词表增删主题时榜数随之变化，不写死 17；
    - 返回顺序固定：语言榜按 LANGUAGES 顺序、"其它语言"收尾，再接主题榜按词表顺序、"其他"收尾，
      页面按列表顺序直出即可，无需自行排序；
    - 分类实时算不物化：语言按 classify_language 归组，主题按 classify_topics 归组——
      命中多主题的仓库在每命中主题榜各出现一次（允许跨榜重复），零命中进"其他"榜；
    - 首周缺席/无快照的仓库不进任何增量榜行；不足 top_n 的榜有多少列多少（不补位）。
    """
    as_of = as_of or utc_now_iso()
    deltas = compute_repo_deltas(conn, period=period, as_of=as_of)
    # 榜单行字段一次取全；dead 剔除下推 SQL，与 compute_repo_deltas 同一过滤口径
    repos = conn.execute(
        "SELECT id, full_name, description_en, language, topics FROM repos WHERE dead = 0"
    ).fetchall()

    lang_buckets: dict[str, list[ReportRow]] = {key: [] for key in (*LANGUAGES.values(), OTHER_LANGUAGE_KEY)}
    topic_buckets: dict[str, list[ReportRow]] = {key: [] for key in (*topic_table, OTHER_TOPIC_KEY)}
    for repo in repos:
        info = deltas.get(repo["id"])
        if info is None or info.absent_reason is not None:
            continue  # 无快照安放不了；首周缺席不进增量榜（total 口径不会缺席）
        row = ReportRow(
            full_name=repo["full_name"],
            description_en=repo["description_en"],
            language=repo["language"],
            stars=info.stars,
            delta=info.delta,
            window_days=info.window_days,
        )
        lang_buckets[classify_language(repo["language"])].append(row)
        # topics 由写入方 json.dumps 落库（schema 默认 '[]'）；单行脏数据直接抛错属 fail-loud
        # 既定姿态（与 _parse_iso 同），不静默兜底吞数据——评审低-3 留痕处置
        hit_keys = classify_topics(json.loads(repo["topics"]), topic_table)
        for key in hit_keys or [OTHER_TOPIC_KEY]:
            topic_buckets[key].append(row)

    boards: list[Board] = [
        Board("language", key, name, _top(lang_buckets[key], period, top_n)) for name, key in LANGUAGES.items()
    ]
    boards.append(Board("language", OTHER_LANGUAGE_KEY, "其它语言", _top(lang_buckets[OTHER_LANGUAGE_KEY], period, top_n)))
    boards.extend(
        Board("topic", key, spec["label"], _top(topic_buckets[key], period, top_n)) for key, spec in topic_table.items()
    )
    boards.append(Board("topic", OTHER_TOPIC_KEY, "其他", _top(topic_buckets[OTHER_TOPIC_KEY], period, top_n)))
    return boards


if __name__ == "__main__":
    # 真实库自检：`python -m app.report [db_path]`——三口径全榜各跑一遍，打印执行计划与每榜行数/样例行。
    # 用途是验证"SQL 在真实数据上跑得通、各代码路径覆盖"，不验证数据语义（口径语义由单测构造数据保证）。
    import sys

    from app.classify import load_topics
    from app.config import BASE_DIR
    from app.db import get_conn

    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    conn = get_conn(db_path)
    try:
        table = load_topics(BASE_DIR / "config" / "topics.yaml")
        as_of = utc_now_iso()
        print(f"库：{db_path or '默认配置（data/radar.db）'}　as_of={as_of}")
        print("端点快照 SQL 执行计划（红线：必须是 SEARCH 索引 seek，不是 SCAN 全表）：")
        for plan_row in conn.execute(f"EXPLAIN QUERY PLAN {_ENDPOINT_SQL}", (1, as_of)):
            print(f"  {plan_row['detail']}")
        for plan_row in conn.execute(f"EXPLAIN QUERY PLAN {_START_AFTER_SQL}", (1, as_of, as_of)):
            print(f"  {plan_row['detail']}")
        deltas_summary = {
            period: compute_repo_deltas(conn, period=period, as_of=as_of) for period in PERIODS
        }
        for period in PERIODS:
            deltas = deltas_summary[period]
            absent = sum(1 for d in deltas.values() if d.absent_reason is not None)
            print(f"\n=== {period}：有快照仓库 {len(deltas)}，缺席 {absent}，出席 {len(deltas) - absent} ===")
            for board in compute_boards(conn, table, period=period, as_of=as_of):
                print(f"[{board.kind}:{board.key}] {board.label} Top{len(board.rows)}")
                for r in board.rows[:3]:
                    delta_text = "——" if r.delta is None else f"{r.delta:+d}"
                    window_text = "" if r.window_days is None else f" 窗口{r.window_days}天"
                    print(f"    {r.full_name} 星{r.stars} 增量{delta_text}{window_text}")
    finally:
        conn.close()
