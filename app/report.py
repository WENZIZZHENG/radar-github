"""榜单计算：周/季增量榜与总星榜（《架构决策记录》决策 3/4 的唯一实现）。

存储形态（T-027 起）：每日采集后 precompute_boards 对三口径各全量算一次、序列化落 board_cache
（JSON blob，schema.sql 建表）；页面打开直读缓存，缓存缺失/过期（load_board_cache 三条判定）
时降级实时算——缓存只换"何时算、结果存哪"，计算口径仍走本模块 compute_boards/compute_repo_deltas，
实时路径一字不改。预计算把 as_of 钉在采集时刻（两次采集之间库内快照不变，"as_of=now"与
"as_of=采集时刻"取同一组端点快照，等价性依据）；代价是当日内"假如此刻实时算"与缓存值在边界仓
起点候选上可能差一个，属可接受且更稳定（消除同日内多次打开间的 now 漂移）。

口径（决策 4 逐字落实，勿自由发挥）：
- 周/季增量 = 两端快照差（端点星数 − 名义窗口天数前端点星数）；
- 端点缺失时滑动取实际可得两端：周接受 5~9 天、季接受 86~94 天跨度，结果标注实际窗口天数；
- 新入池首周缺席增量榜：不满最小窗口跨度（含只有一个快照）一律缺席，绝不拿单日增量混排；
- 新崛起区（决策 4 v2）：在池跨度不足最小窗口的缺席仓（真·新入池）进新区——在池增量 = 端点星数 −
  最旧基线快照星数，在池天数 = 两端间隔，按在池增量降序 Top 10 分区展示（与主榜不混排）；
  采集停机致跨窗空洞（在池天数 > 窗口上限）的缺席老仓不进新区，回 v1 两不见（F2-1 收窄）；
- 新项目区（T-033 决策 4）：出席仓中 GitHub 创建未满 1 年（as_of − github_created_at < 365 天）且未进
  本榜主榜 Top 50 的仓，周/季按窗口增量、total 按总星降序 Top 20 分区展示（与主榜不混排）；
  github_created_at 未回填（NULL）一律不进；缺席仓归新崛起区、出席仓才有机会进本区，两区天然互斥；
  同一仓库在同一张榜内三区只出现一次（与主榜按 full_name 去重）；
- 负增量正常参与排序（降序下自然沉底）；dead=1 仓库全部剔除；
- 总星榜 = as_of 之前最新快照星数降序，无增量、无窗口概念。

SQL 红线（schema.sql 硬约定，T-002 评审 EXPLAIN 实测整表扫为 2300 万行级）：
端点快照查询必须从 repos 驱动、per-repo `WHERE repo_id=? AND captured_at<=? ORDER BY captured_at DESC LIMIT 1`
走主键 (repo_id, captured_at) 索引 seek；禁止对 star_snapshots 整表 GROUP BY 或相关子查询全扫。
本模块逐 alive 仓库做 ≤3 次 LIMIT 1 索引 seek（end、名义起点前最近、名义起点后最早）；
新崛起区每缺席仓另做 1 次最旧基线快照 seek（_BASELINE_SQL，同红线风格）。
6.4 万 repo × 每口径 ≤3 次 seek 可接受，换取执行计划恒定无全表扫。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone

from app.classify import LANGUAGES, OTHER_LANGUAGE_KEY, TopicSpec, classify_language, classify_topics
from app.collector.github import utc_now_iso

# 主题零命中仓库的归组 key：classify 层刻意不发明该 key（空列表语义留给调用方），榜单层在此统一定义
OTHER_TOPIC_KEY = "other"

# 增量口径参数：(名义窗口天数, 滑动窗口下限, 滑动窗口上限)。
# 周 5~9 天、季 86~94 天均为决策 4 钉死（v2 起含新崛起区口径）
_INCREMENT_WINDOWS: dict[str, tuple[int, int, int]] = {
    "week": (7, 5, 9),
    "quarter": (90, 86, 94),
}
PERIODS: tuple[str, ...] = ("week", "quarter", "total")

ABSENT_FIRST_WEEK = "first_week"  # 缺席原因：不满最小窗口跨度（含只有一个快照），口径上统称"首周缺席"

_RISING_TOP_N = 10  # 新崛起区每分类榜行数上限（决策 4 v2 钉死：Top 10，不足按实际）
_FRESH_MAX_AGE_DAYS = 365  # 新项目区准入：GitHub 创建满 1 年即出区（T-033 决策 3，天数差口径同窗口）
_FRESH_ZONE_TOP_N = 20  # 新项目区每分类榜行数上限（T-033 决策 4：Top 20，不足按实际）

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # schema 硬约定：UTC 定长，字典序即时间序


def week_label(d: date) -> str:
    """date → ISO 周标签（%G-W%V 定宽零填充，字符串比较即时间序）。

    T-027 起为期次标签单一事实源：precompute_boards 落表 label 与页面期次换算（routes 同名私有别名）
    共用本函数，格式口径（2026-W33 形态）不得改动——recommendations.period_label 同款标签依赖此格式。
    """
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def quarter_label(d: date) -> str:
    """date → 季标签（2026-Q3 形态）。T-027 单一事实源，理由同 week_label。"""
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"

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

# 新崛起区基线快照 SQL（决策 4 v2）：入池基线 = 该仓最旧一张快照，主键 (repo_id, captured_at) 索引 seek，
# 每缺席仓一次 LIMIT 1；tests 以 EXPLAIN QUERY PLAN 锁定其索引 seek 性质（与 _ENDPOINT_SQL 同款红线）
_BASELINE_SQL = (
    "SELECT captured_at, stars FROM star_snapshots "
    "WHERE repo_id = ? ORDER BY captured_at ASC LIMIT 1"
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
    captured_at: str | None = None  # 端点（最近）快照时间（UTC 定长 ISO）；T-018 新区在池天数口径用


@dataclass(frozen=True)
class ReportRow:
    """榜单行：T-008 页面层每行需要的最小字段集（frozen，可安全跨榜共享同一实例）。

    created_year（T-033）：GitHub 创建年份（github_created_at 前 4 位），NULL 未回填 → None（模板不渲染标注）。
    """

    full_name: str
    description_en: str | None
    language: str | None  # GitHub 原始语言名（可能 None），页面展示用；归组 key 在 Board 上
    stars: int  # 总星数（端点快照星数）
    delta: int | None  # 增量；总星榜恒 None
    window_days: float | None  # 实际窗口跨度天数；总星榜恒 None；首周缺席项目不出现在榜行中
    captured_at: str | None = None  # 端点（最近）快照时间（UTC 定长 ISO）；T-020 页面端点日期标注用，透传不计算
    created_year: int | None = None  # T-033：GitHub 创建年份标注数据源，透传不计算


@dataclass(frozen=True)
class RisingRow:
    """新崛起区行（决策 4 v2）：主榜出席校验失败且在池天数 < 最小窗口（真·新入池）的缺席仓，按在池增量排序分区展示。

    created_year（T-033）：GitHub 创建年份，NULL 未回填 → None（模板不渲染标注）。
    """

    full_name: str
    description_en: str | None
    language: str | None  # GitHub 原始语言名（可能 None），页面展示用；归组 key 在 Board 上
    stars: int  # 端点星数（同主榜行"总星数"展示）
    pool_delta: int  # 在池增量 = 端点星数 − 入池基线（最旧一张快照）星数；允许为负
    pool_days: float  # 在池天数 = 端点与基线两端间隔（天，保留 1 位小数同 window_days 精度）
    captured_at: str | None = None  # 端点（最近）快照时间（UTC 定长 ISO）；T-020 页面端点日期标注用，透传不计算
    created_year: int | None = None  # T-033：GitHub 创建年份标注数据源，透传不计算


@dataclass(frozen=True)
class Board:
    """一张榜：kind+key 是机器标识（页面锚点/路由用），label 是展示名，rows 已按口径排序并截 Top N。

    rising_rows（T-018 决策 4 v2）：新崛起区行——在池跨度不足最小窗口的缺席仓按在池增量降序 Top 10，
    分区展示不与主榜混排；跨窗空洞缺席老仓不收（准入判定见 compute_boards）；total 口径恒空（无缺席概念）。
    fresh（T-033 决策 5）：新项目区行——出席且创建 < 365 天且不在本榜主榜 Top 50 的仓，周/季按窗口增量、
    total 按总星降序 Top 20；未回填（NULL）不进；total 口径有本区（无缺席概念，准入仅年龄）；缺省空列表。
    count（T-026 单榜整页 §13.1）：full_keys 模式下非当前榜只归桶计数不建行对象——rows 空、count=主榜行数，
    供边栏徽标；全量模式恒 None（徽标直接用 len(rows)）。
    """

    kind: str  # "language" | "topic"
    key: str  # 语言 key（LANGUAGES 值/other）或主题 key（词表 key/other）
    label: str  # 展示名：语言用 GitHub 精确名（"Java"…），主题用词表 label，两个兜底榜为"其它语言"/"其他"
    rows: list[ReportRow]
    rising_rows: list[RisingRow] = field(default_factory=list)
    fresh: list[ReportRow] = field(default_factory=list)  # T-033：新项目区行（放 rising 后）
    count: int | None = None  # T-026：单榜整页模式下非当前榜的主榜行数；None = 全量模式


def _parse_iso(ts: str) -> datetime:
    """按 schema 定长硬约定解析；带偏移或缺秒的变体直接报错，不静默容错（比较语义全靠定长）。"""
    return datetime.strptime(ts, _ISO_FORMAT).replace(tzinfo=timezone.utc)


def _created_year(ts: str | None) -> int | None:
    """github_created_at（定长 ISO，schema 硬约定）→ 年份；NULL → None（模板不渲染标注）。

    前 4 位即年份（定长硬约定，字典序即时间序）；畸形值 int() 抛 ValueError fail-loud
    （写入方归一化保证，与 _parse_iso 同姿态）。
    """
    if ts is None:
        return None
    return int(ts[:4])


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
            result[repo_id] = RepoDelta(
                stars=end["stars"], delta=None, window_days=None, absent_reason=None, captured_at=end["captured_at"]
            )
        else:
            info = _increment_delta(conn, repo_id, end, as_of_dt, period)
            # T-018：新区在池天数口径需要端点快照时间（缺席仓取最旧基线时求两端间隔）；出席仓同样携带，语义统一
            result[repo_id] = replace(info, captured_at=end["captured_at"])
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


def pool_days_label(pool_days: float) -> int:
    """在池天数整数化（T-018 授权实施：round 取整）：页面"入池 N 天"标注与 AI 推荐语 prompt 共用同一取值，防两处口径漂移。"""
    return round(pool_days)


def _rising_top(rows: list[RisingRow], top_n: int) -> list[RisingRow]:
    """新区排序：在池增量降序为第一键，负增量自然沉底；星数、全名作次序键保证结果可复现（与 _top 同构）。"""
    ordered = sorted(rows, key=lambda r: (-r.pool_delta, -r.stars, r.full_name))
    return ordered[:top_n]


def _fresh_zone(
    bucket: list[ReportRow],
    main_names: set[str],
    created_by_name: dict[str, str | None],
    as_of_dt: datetime,
    period: str,
) -> list[ReportRow]:
    """新项目区（T-033 决策 4）：出席（bucket 恒出席）∧ 创建 < 365 天 ∧ 不在本榜主榜 Top 50，按口径 Top 20。

    - 主榜截断先做（main_names 由调用方传主榜 Top50 的 full_name），排除后剩余候选再排序——兑现
      "凭实力进主榜则不在新区重影"的三段流水线语义；
    - github_created_at 为 NULL（未回填）的仓一律不进：NULL 语义是"未知"，未知不冒充新项目；
    - 年龄 = as_of − github_created_at 天数差（.days 下取整），满 365 天即出区（边界：差 364.999 天进、365 天不进）；
    - 排序与截断复用 _top（周/季按增量、total 按总星，次序键同主榜），不足按实际不补位。
    """
    fresh = []
    for r in bucket:
        if r.full_name in main_names:
            continue
        created = created_by_name.get(r.full_name)
        if created is None:
            continue
        # _parse_iso 同款 fail-loud：库内 github_created_at 由采集层归一化写入，畸形即数据事故
        created_dt = _parse_iso(created)
        if (as_of_dt - created_dt).days >= _FRESH_MAX_AGE_DAYS:
            continue
        fresh.append(r)
    return _top(fresh, period, _FRESH_ZONE_TOP_N)


def compute_boards(
    conn: sqlite3.Connection,
    topic_table: dict[str, TopicSpec],
    *,
    period: str = "week",
    as_of: str | None = None,
    top_n: int = 50,
    full_keys: Collection[str] | None = None,
) -> list[Board]:
    """算指定口径的全部分类榜：语言榜 7 张 + 主题榜（词表主题数 + 1）张，每榜 Top top_n 行。

    - topic_table 由调用方 load_topics() 后传入（与 classify.py 同口径：词表路径决策留在调用方）；
    - 当前封板词表 9 主题 → 共 17 张榜（决策 3）；词表增删主题时榜数随之变化，不写死 17；
    - 返回顺序固定：语言榜按 LANGUAGES 顺序、"其它语言"收尾，再接主题榜按词表顺序、"其他"收尾，
      页面按列表顺序直出即可，无需自行排序；
    - 分类实时算不物化：语言按 classify_language 归组，主题按 classify_topics 归组——
      命中多主题的仓库在每命中主题榜各出现一次（允许跨榜重复），零命中进"其他"榜；
    - 首周缺席/无快照的仓库不进任何增量榜行；不足 top_n 的榜有多少列多少（不补位）；
    - 新崛起区（决策 4 v2）：准入＝在池跨度不足最小窗口的缺席仓（ABSENT_FIRST_WEEK 且 pool_days < win_min），
      按语言/主题同主榜归组、在池增量降序 Top 10（_RISING_TOP_N）；跨窗空洞缺席老仓（pool_days > win_max）
      不进新区（回 v1 两不见）；total 口径无缺席概念恒空；
    - 新项目区（T-033 决策 4）：出席仓中 github_created_at 非 NULL 且 as_of − 创建时间 < 365 天
      （_FRESH_MAX_AGE_DAYS）且不在本榜主榜 Top 50（先 _top 出主榜再按 full_name 排除），按语言/主题
      同主榜归组、周/季按窗口增量、total 按总星降序 Top 20（_FRESH_ZONE_TOP_N）；跨榜重复与主榜同口径；
      三区互斥：缺席仓归新崛起区、出席仓才有机会进新项目区、进主榜即从新区剔除——同仓同榜只出现一次；
    - full_keys（T-026 单榜整页 §13.1）：非 None 时仅指定榜 key（"kind-key" 形态，如 "language-java"）的榜
      构建主榜行对象（rows），其余榜只归桶计数（rows 空、count=min(出席数, top_n)——与全量模式徽标
      len(rows) 截断语义一致，两模式徽标不因 >top_n 漂移）——单榜模式的边栏徽标数据源；省的是 _top 排序＋
      行视图装配＋模板渲染（传输体积），归桶与 compute_repo_deltas 仍全量（徽标计数/降级判定需要）；
      新崛起区行保持全量计算（缺席仓量级小，且降级判定"主榜＋新崛起区＋新项目区全空才降级"需要全库
      新区信息）；新项目区按全量计算、装配时仅指定榜取用（其余榜 fresh 恒空列表、不计数——T-033 决策 6）；
      None = 既有全量行为（count 恒 None）。
    """
    as_of = as_of or utc_now_iso()
    as_of_dt = _parse_iso(as_of)
    deltas = compute_repo_deltas(conn, period=period, as_of=as_of)
    # 榜单行字段一次取全；dead 剔除下推 SQL，与 compute_repo_deltas 同一过滤口径。
    # github_created_at（T-033）供 created_year 标注与新项目区年龄准入；repos 小表全列取无 SQL 红线问题
    repos = conn.execute(
        "SELECT id, full_name, description_en, language, topics, github_created_at FROM repos WHERE dead = 0"
    ).fetchall()
    # 新项目区按 full_name 反查创建时间：同库 full_name 唯一（repos UNIQUE 索引），去重安全（决策 4）
    created_by_name = {repo["full_name"]: repo["github_created_at"] for repo in repos}

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
            captured_at=info.captured_at,  # T-020：端点日期标注数据源，透传不计算
            created_year=_created_year(repo["github_created_at"]),  # T-033：创建年份标注数据源，透传不计算
        )
        lang_buckets[classify_language(repo["language"])].append(row)
        # topics 由写入方 json.dumps 落库（schema 默认 '[]'）；单行脏数据直接抛错属 fail-loud
        # 既定姿态（与 _parse_iso 同），不静默兜底吞数据——评审低-3 留痕处置
        hit_keys = classify_topics(json.loads(repo["topics"]), topic_table)
        for key in hit_keys or [OTHER_TOPIC_KEY]:
            topic_buckets[key].append(row)

    # T-018 新崛起区（决策 4 v2）：在池跨度不足最小窗口的缺席仓 → 按语言/主题归桶、在池增量 Top 10。
    # total 口径无缺席概念（deltas 无 absent_reason），新区恒空——不计算即不渲染。
    rising_lang: dict[str, list[RisingRow]] = {key: [] for key in lang_buckets}
    rising_topic: dict[str, list[RisingRow]] = {key: [] for key in topic_buckets}
    if period != "total":
        win_min = _INCREMENT_WINDOWS[period][1]
        for repo in repos:
            info = deltas.get(repo["id"])
            if info is None or info.absent_reason is None or info.captured_at is None:
                continue  # 无端点安放不了；出席仓不进新区（毕业自然离开主榜缺席集，无需特判）
            base = conn.execute(_BASELINE_SQL, (repo["id"],)).fetchone()  # 每缺席仓 ≤1 次额外 seek
            end_dt = _parse_iso(info.captured_at)
            base_dt = _parse_iso(base["captured_at"])
            pool_days = round((end_dt - base_dt).total_seconds() / 86400, 1)
            # 缺席分两向（k3 评审 F2-1 处置，对齐决策 4 v2 字面）：在池天数 < win_min（真·新入池）进新区；
            # > win_max（采集停机致跨窗空洞）不进新区——回 v1 不可见，全空时走首期降级，区头"入池不足 N 天"文案永真。
            # 另有第三类边缘缺席（评审新 F3-a）：pool_days 落 [win_min, win_max] 但基线非最近起点候选
            # （复合空洞），同按字面不进新区——gate 只看 pool_days < win_min，三种缺席行为一致可对。
            if pool_days >= win_min:
                continue
            row = RisingRow(
                full_name=repo["full_name"],
                description_en=repo["description_en"],
                language=repo["language"],
                stars=info.stars,
                pool_delta=info.stars - base["stars"],
                pool_days=pool_days,
                captured_at=info.captured_at,  # T-020：端点日期标注数据源，透传不计算
                created_year=_created_year(repo["github_created_at"]),  # T-033：创建年份标注数据源，透传不计算
            )
            rising_lang[classify_language(repo["language"])].append(row)
            hit_keys = classify_topics(json.loads(repo["topics"]), topic_table)
            for key in hit_keys or [OTHER_TOPIC_KEY]:
                rising_topic[key].append(row)

    rising_lang_top = {key: _rising_top(rows, _RISING_TOP_N) for key, rows in rising_lang.items()}
    rising_topic_top = {key: _rising_top(rows, _RISING_TOP_N) for key, rows in rising_topic.items()}
    # T-033 新项目区（决策 4）：每桶先 _top 出主榜 Top top_n，再从剩余出席且创建 < 365 天的仓里
    # 按同排序键取 Top 20（_fresh_zone 内部完成排除＋截断）；三口径都有本区（total 无缺席概念，
    # 准入仅年龄∧不在主榜）；跨榜重复与主榜同口径（每命中榜各出现一次）。
    fresh_lang_top = {
        key: _fresh_zone(rows, {r.full_name for r in _top(rows, period, top_n)}, created_by_name, as_of_dt, period)
        for key, rows in lang_buckets.items()
    }
    fresh_topic_top = {
        key: _fresh_zone(rows, {r.full_name for r in _top(rows, period, top_n)}, created_by_name, as_of_dt, period)
        for key, rows in topic_buckets.items()
    }
    # T-026 单榜整页（§13.1）：full_keys 非 None 时仅指定榜构建主榜行对象，其余榜只归桶计数
    # （rows 空、count=主榜行数，min(len, top_n) 与全量模式徽标 len(rows) 同语义——_top 截断后长度
    # 恰为 min(出席数, top_n)，两模式徽标不因 >50 仓/榜漂移）；新区行全量（缺席仓量级小，
    # 且降级判定"主榜＋新崛起区＋新项目区全空才降级"需要全库新区信息，不能按 full_keys 收窄）；
    # 新项目区按全量算、装配时仅指定榜取用（其余榜 fresh 恒空列表——T-033 决策 6）
    def _full(kind: str, key: str) -> bool:
        return full_keys is None or f"{kind}-{key}" in full_keys

    def _board(kind: str, key: str, label: str, lang: bool) -> Board:
        bucket = lang_buckets[key] if lang else topic_buckets[key]
        rising = rising_lang_top[key] if lang else rising_topic_top[key]
        fresh = fresh_lang_top[key] if lang else fresh_topic_top[key]
        if _full(kind, key):
            return Board(kind, key, label, _top(bucket, period, top_n), rising, fresh)
        return Board(kind, key, label, [], rising, [], count=min(len(bucket), top_n))

    boards: list[Board] = [_board("language", key, name, True) for name, key in LANGUAGES.items()]
    boards.append(_board("language", OTHER_LANGUAGE_KEY, "其它语言", True))
    boards.extend(_board("topic", key, spec["label"], False) for key, spec in topic_table.items())
    boards.append(_board("topic", OTHER_TOPIC_KEY, "其他", False))
    return boards


# ===== T-027 榜单预计算缓存：board_cache 表读写 =====
# payload 为 list[Board] 的 JSON 序列化，字段白名单手工展开（不用 dataclasses.asdict 一把梭：
# 防字段漂移静默丢——新增字段未写进白名单时反序列化缺键直接报错 fail-loud，而不是静默变 None）。


def _board_to_payload(boards: list[Board]) -> str:
    """list[Board] → JSON 字符串。Board.count 不存（全量模式恒 None，还原时置 None）。

    T-033：白名单显式展开 fresh 与 created_year（手工白名单风格不变——新增字段未写进白名单时
    反序列化缺键直接报错 fail-loud）。
    """
    return json.dumps(
        [
            {
                "kind": b.kind,
                "key": b.key,
                "label": b.label,
                "rows": [
                    {
                        "full_name": r.full_name,
                        "description_en": r.description_en,
                        "language": r.language,
                        "stars": r.stars,
                        "delta": r.delta,
                        "window_days": r.window_days,
                        "captured_at": r.captured_at,
                        "created_year": r.created_year,
                    }
                    for r in b.rows
                ],
                "rising_rows": [
                    {
                        "full_name": r.full_name,
                        "description_en": r.description_en,
                        "language": r.language,
                        "stars": r.stars,
                        "pool_delta": r.pool_delta,
                        "pool_days": r.pool_days,
                        "captured_at": r.captured_at,
                        "created_year": r.created_year,
                    }
                    for r in b.rising_rows
                ],
                "fresh": [
                    {
                        "full_name": r.full_name,
                        "description_en": r.description_en,
                        "language": r.language,
                        "stars": r.stars,
                        "delta": r.delta,
                        "window_days": r.window_days,
                        "captured_at": r.captured_at,
                        "created_year": r.created_year,
                    }
                    for r in b.fresh
                ],
            }
            for b in boards
        ],
        ensure_ascii=False,
    )


def _board_from_payload(raw: str) -> list[Board]:
    """JSON 字符串 → list[Board]（frozen dataclass，与 compute_boards 返回同型，count 恒 None）。

    字段显式展开不撒 **row：白名单缺字段时 KeyError fail-loud，与序列化白名单互为镜像。
    T-033：旧缓存缺 fresh/created_year 键按此口径报错（部署后重跑预计算刷新）。
    """
    boards = []
    for item in json.loads(raw):
        boards.append(
            Board(
                kind=item["kind"],
                key=item["key"],
                label=item["label"],
                rows=[
                    ReportRow(
                        full_name=row["full_name"],
                        description_en=row["description_en"],
                        language=row["language"],
                        stars=row["stars"],
                        delta=row["delta"],
                        window_days=row["window_days"],
                        captured_at=row["captured_at"],
                        created_year=row["created_year"],
                    )
                    for row in item["rows"]
                ],
                rising_rows=[
                    RisingRow(
                        full_name=row["full_name"],
                        description_en=row["description_en"],
                        language=row["language"],
                        stars=row["stars"],
                        pool_delta=row["pool_delta"],
                        pool_days=row["pool_days"],
                        captured_at=row["captured_at"],
                        created_year=row["created_year"],
                    )
                    for row in item["rising_rows"]
                ],
                fresh=[
                    ReportRow(
                        full_name=row["full_name"],
                        description_en=row["description_en"],
                        language=row["language"],
                        stars=row["stars"],
                        delta=row["delta"],
                        window_days=row["window_days"],
                        captured_at=row["captured_at"],
                        created_year=row["created_year"],
                    )
                    for row in item["fresh"]
                ],
            )
        )
    return boards


def save_board_cache(conn: sqlite3.Connection, *, period: str, label: str, as_of: str, boards: list[Board]) -> None:
    """序列化并落一行缓存（INSERT OR REPLACE：同日重跑幂等覆盖，与快照同口径）。

    本函数不提交事务，由调用方 commit（precompute_boards 每口径落一行即 commit，部分失败不丢已算好的口径）；
    页面层无写入入口，只服务预计算路径。period 必须是 PERIODS 之一（'week'/'quarter'/'total'）。
    """
    if period not in PERIODS:
        raise ValueError(f"未知榜单口径：{period!r}，可选 {PERIODS}")
    conn.execute(
        "INSERT OR REPLACE INTO board_cache (period, label, as_of, payload, computed_at) VALUES (?, ?, ?, ?, ?)",
        (period, label, as_of, _board_to_payload(boards), utc_now_iso()),
    )


def load_board_cache(conn: sqlite3.Connection, *, period: str, label: str) -> list[Board] | None:
    """读一行缓存并还原 list[Board]；三条降级判定任一不过返回 None（调用方走实时算路径，不白屏）：

    ① 表无该 period 行（未预计算/预计算失败）；
    ② 缓存 label 与请求 label 不符（跨周/跨季凌晨窗口：旧期缓存未覆盖当前期；total 恒 'all' 不受影响）；
    ③ 库内 MAX(captured_at) 晚于缓存 as_of（采集写了新快照但预计算未跑/失败——新快照口径未落缓存，
       缓存数据已过期；定长 ISO 字典序比较，schema 硬约定）。
    命中则反序列化还原（frozen dataclass，与 compute_boards 返回同型、count 恒 None）。
    """
    row = conn.execute("SELECT label, as_of, payload FROM board_cache WHERE period = ?", (period,)).fetchone()
    if row is None:
        return None
    if row["label"] != label:
        return None
    latest = conn.execute("SELECT MAX(captured_at) FROM star_snapshots").fetchone()[0]
    if latest is not None and latest > row["as_of"]:
        return None
    return _board_from_payload(row["payload"])


def precompute_boards(
    conn: sqlite3.Connection, topic_table: dict[str, TopicSpec], *, as_of: str | None = None
) -> dict:
    """每日采集后调用：同一 as_of（缺省 utc_now_iso()）对 PERIODS 三口径各 compute_boards 全量一次并落表。

    - as_of 钉在采集时刻（调用方传 run_daily 完成时刻）：两次采集之间库内快照不变，页面读缓存与
      "此刻实时算"取同一组端点快照；as_of 晚于本轮快照 captured_at 时判定③恒过（同日不误判过期）；
    - label 按 as_of 日期换算（week_label/quarter_label；total 固定 'all'），与页面期次换算同一事实源；
    - 每口径落一行即 commit（save_board_cache 不提交事务）：部分口径失败不丢已算好的行，
      页面按 period 独立降级（缺哪口径实时算哪口径）；
    - 返回摘要 dict {period: {"boards": 榜数, "rows": 主榜总行数, "rising": 新崛起区总行数}} 供调用方记日志。
    """
    as_of = as_of or utc_now_iso()
    as_of_date = date.fromisoformat(as_of[:10])
    labels = {"week": week_label(as_of_date), "quarter": quarter_label(as_of_date), "total": "all"}
    summary: dict[str, dict[str, int]] = {}
    for period in PERIODS:
        boards = compute_boards(conn, topic_table, period=period, as_of=as_of)
        save_board_cache(conn, period=period, label=labels[period], as_of=as_of, boards=boards)
        conn.commit()
        summary[period] = {
            "boards": len(boards),
            "rows": sum(len(b.rows) for b in boards),
            "rising": sum(len(b.rising_rows) for b in boards),
        }
    return summary


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
        for plan_row in conn.execute(f"EXPLAIN QUERY PLAN {_BASELINE_SQL}", (1,)):
            print(f"  {plan_row['detail']}")
        deltas_summary = {
            period: compute_repo_deltas(conn, period=period, as_of=as_of) for period in PERIODS
        }
        for period in PERIODS:
            deltas = deltas_summary[period]
            absent = sum(1 for d in deltas.values() if d.absent_reason is not None)
            print(f"\n=== {period}：有快照仓库 {len(deltas)}，缺席 {absent}，出席 {len(deltas) - absent} ===")
            for board in compute_boards(conn, table, period=period, as_of=as_of):
                print(f"[{board.kind}:{board.key}] {board.label} Top{len(board.rows)} 新区{len(board.rising_rows)} 新项目{len(board.fresh)}")
                for r in board.rows[:3]:
                    delta_text = "——" if r.delta is None else f"{r.delta:+d}"
                    window_text = "" if r.window_days is None else f" 窗口{r.window_days}天"
                    year_text = "" if r.created_year is None else f" 创建{r.created_year}"
                    print(f"    {r.full_name} 星{r.stars} 增量{delta_text}{window_text}{year_text}")
                for r in board.rising_rows[:3]:
                    print(f"    新区 {r.full_name} 星{r.stars} 在池{r.pool_delta:+d}（{r.pool_days:g}天）")
                for r in board.fresh[:3]:
                    print(f"    新项目 {r.full_name} 星{r.stars} 创建{r.created_year}")
    finally:
        conn.close()
