"""榜单口径测试：tmp_path 真实文件库（WAL 对 :memory: 不生效，与 test_db 同口径）。

口径语义全部靠这里的构造数据覆盖（真实库快照历史太短，验证不了窗口语义）：
滑动窗口 5~9 天两端取数、新入池首周缺席、负增量沉底、dead 剔除、Top N 不足按实际、
跨主题重复、零命中进"其他"、"其它语言"归组、总星榜口径。
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.classify import LANGUAGES, load_topics
from app.config import BASE_DIR
from app.db import get_conn, init_db
from app.report import (
    _BASELINE_SQL,
    _ENDPOINT_SQL,
    _START_AFTER_SQL,
    ABSENT_FIRST_WEEK,
    compute_boards,
    compute_repo_deltas,
)

TOPICS_PATH = BASE_DIR / "config" / "topics.yaml"
AS_OF = "2026-08-09T00:00:00Z"  # 固定基准时刻：窗口语义不依赖"今天"，测试可复现
_AS_OF_DT = datetime(2026, 8, 9, tzinfo=timezone.utc)


def _iso(days_before: float) -> str:
    """as_of 之前 days_before 天的定长 UTC 时间戳。"""
    return (_AS_OF_DT - timedelta(days=days_before)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_repo(conn, name, *, language=None, topics=(), dead=0, snapshots=(), github_created_at=None):
    """插一个仓库及其快照，返回 repo_id；snapshots 为 (captured_at, stars) 列表。

    github_created_at（T-033）：GitHub 创建时间（定长 ISO），缺省 None（NULL 未回填）。
    """
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, language, topics, dead, source, created_at, github_created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, f"node-{name}", language, json.dumps(list(topics)), dead, "test", "2026-07-01T00:00:00Z", github_created_at),
    )
    repo_id = cur.lastrowid
    for captured_at, stars in snapshots:
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, captured_at, stars),
        )
    return repo_id


def _board(boards, kind, key):
    return next(b for b in boards if b.kind == kind and b.key == key)


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    c = get_conn(db)
    yield c
    c.close()


@pytest.fixture()
def topic_table():
    return load_topics(TOPICS_PATH)


def test_sliding_window_boundaries(conn, topic_table):
    """5~9 天跨度接受并标注实际窗口，4/10 天缺席；取数用实际可得两端，不要求是名义端点。"""
    repo_w5 = _add_repo(conn, "a/w5", language="Python", snapshots=[(_iso(5), 100), (_iso(0), 160)])
    repo_w7 = _add_repo(conn, "a/w7", language="Python", snapshots=[(_iso(7), 100), (_iso(0), 180)])
    repo_w9 = _add_repo(conn, "a/w9", language="Python", snapshots=[(_iso(9), 100), (_iso(0), 200)])
    # 名义端点缺失滑动取数：end 缺 T 日快照（最近可得为 T-2d），start 准点 → 窗口 5 天
    repo_slide = _add_repo(conn, "a/slide", language="Python", snapshots=[(_iso(7), 100), (_iso(2), 150)])
    repo_narrow = _add_repo(conn, "a/too-narrow", language="Python", snapshots=[(_iso(4), 100), (_iso(0), 130)])
    repo_wide = _add_repo(conn, "a/too-wide", language="Python", snapshots=[(_iso(10), 100), (_iso(0), 130)])
    # 起点两侧候选都在窗口内：取离名义起点更近的一张（T-6 距 T-7 一天，T-9 距两天）
    repo_two = _add_repo(
        conn, "a/two-candidates", language="Python", snapshots=[(_iso(9), 100), (_iso(6), 200), (_iso(0), 500)]
    )

    deltas = compute_repo_deltas(conn, period="week", as_of=AS_OF)
    assert deltas[repo_w5].delta == 60 and deltas[repo_w5].window_days == 5.0
    assert deltas[repo_w7].delta == 80 and deltas[repo_w7].window_days == 7.0
    assert deltas[repo_w9].delta == 100 and deltas[repo_w9].window_days == 9.0
    # 滑动取数：增量必须是实际两端（T-2d 与 T-7d）之差，窗口标注实际跨度 5 天
    assert deltas[repo_slide].delta == 50 and deltas[repo_slide].window_days == 5.0
    # 跨度滑出 5~9 天区间一律缺席
    assert deltas[repo_narrow].absent_reason == ABSENT_FIRST_WEEK
    assert deltas[repo_wide].absent_reason == ABSENT_FIRST_WEEK
    # 两候选择优：起点取 T-6d（增量 500-200=300，窗口 6 天），不是 T-9d
    assert deltas[repo_two].delta == 300 and deltas[repo_two].window_days == 6.0
    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    by_name = {r.full_name: r for r in _board(boards, "language", "python").rows}
    assert set(by_name) == {"a/w5", "a/w7", "a/w9", "a/slide", "a/two-candidates"}
    assert by_name["a/slide"].window_days == 5.0


def test_first_week_absent(conn, topic_table):
    """只有一个快照（不满 5 天跨度）的新入池项目当周缺席，不得以单日增量混排。"""
    repo_new = _add_repo(conn, "a/newbie", language="Go", snapshots=[(_iso(1), 500)])
    repo_old = _add_repo(conn, "a/veteran", language="Go", snapshots=[(_iso(7), 100), (_iso(0), 300)])

    deltas = compute_repo_deltas(conn, period="week", as_of=AS_OF)
    assert deltas[repo_new].delta is None
    assert deltas[repo_new].absent_reason == ABSENT_FIRST_WEEK
    assert deltas[repo_old].absent_reason is None

    go_rows = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "go").rows
    assert [r.full_name for r in go_rows] == ["a/veteran"]


def test_negative_delta_sinks_to_bottom(conn, topic_table):
    """负增量允许上榜但降序下自然沉底。"""
    _add_repo(conn, "a/up", language="Rust", snapshots=[(_iso(7), 100), (_iso(0), 300)])  # +200
    _add_repo(conn, "a/flat-up", language="Rust", snapshots=[(_iso(7), 100), (_iso(0), 110)])  # +10
    _add_repo(conn, "a/down", language="Rust", snapshots=[(_iso(7), 500), (_iso(0), 450)])  # -50

    rows = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "rust").rows
    assert [r.full_name for r in rows] == ["a/up", "a/flat-up", "a/down"]
    assert rows[-1].delta == -50


def test_dead_repo_excluded(conn, topic_table):
    """dead=1 仓库即使两端快照齐全也剔除：不进 deltas、不进任何榜。"""
    repo_dead = _add_repo(conn, "a/ghost", language="Java", dead=1, snapshots=[(_iso(7), 100), (_iso(0), 900)])
    _add_repo(conn, "a/alive", language="Java", snapshots=[(_iso(7), 100), (_iso(0), 200)])

    assert repo_dead not in compute_repo_deltas(conn, period="week", as_of=AS_OF)
    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    assert [r.full_name for r in _board(boards, "language", "java").rows] == ["a/alive"]
    assert all(r.full_name != "a/ghost" for b in boards for r in b.rows)


def test_top_n_short_board_and_limit(conn, topic_table):
    """不足 30 有多少列多少；超过 30 截 Top 30，被截掉的是增量最小者。

    T-028：默认 top_n 已改 50，本测显式 top_n=30 钉"截断语义钉在显式参数上"，与默认值脱钩。
    """
    for i in range(1, 32):  # 31 个 Python 仓库，增量 1..31
        _add_repo(conn, f"a/py-{i:02d}", language="Python", snapshots=[(_iso(7), 1000), (_iso(0), 1000 + i)])
    _add_repo(conn, "a/go-1", language="Go", snapshots=[(_iso(7), 100), (_iso(0), 200)])
    _add_repo(conn, "a/go-2", language="Go", snapshots=[(_iso(7), 100), (_iso(0), 150)])

    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF, top_n=30)
    py_rows = _board(boards, "language", "python").rows
    assert len(py_rows) == 30
    assert py_rows[0].delta == 31
    assert {r.delta for r in py_rows} == set(range(2, 32))  # 增量 1 的被截掉
    assert len(_board(boards, "language", "go").rows) == 2  # 不足 30 按实际数量


def test_multi_topic_repo_repeats_across_boards(conn, topic_table):
    """命中多主题的仓库在每个命中主题榜都出现（跨榜重复是口径，不是 bug）。"""
    _add_repo(conn, "a/ai-react", topics=["ai", "react"], snapshots=[(_iso(7), 100), (_iso(0), 300)])

    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    ai_names = [r.full_name for r in _board(boards, "topic", "ai").rows]
    frontend_names = [r.full_name for r in _board(boards, "topic", "frontend").rows]
    assert "a/ai-react" in ai_names and "a/ai-react" in frontend_names


def test_zero_topic_hit_goes_to_other_board(conn, topic_table):
    """topics 零命中（含空 topics）进"其他"主题榜，且不出现在 9 个词表主题榜。"""
    _add_repo(conn, "a/niche", topics=["obscure-xyz"], snapshots=[(_iso(7), 100), (_iso(0), 300)])
    _add_repo(conn, "a/notopics", snapshots=[(_iso(7), 100), (_iso(0), 200)])

    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    other_names = [r.full_name for r in _board(boards, "topic", "other").rows]
    assert set(other_names) == {"a/niche", "a/notopics"}
    for key in topic_table:
        assert all(r.full_name not in {"a/niche", "a/notopics"} for r in _board(boards, "topic", key).rows)


def test_other_language_grouping(conn, topic_table):
    """未列出语言与无语言仓库都归"其它语言"榜，不进 6 个指定语言榜。"""
    _add_repo(conn, "a/kotlin-app", language="Kotlin", snapshots=[(_iso(7), 100), (_iso(0), 300)])
    _add_repo(conn, "a/no-lang", language=None, snapshots=[(_iso(7), 100), (_iso(0), 200)])
    _add_repo(conn, "a/java-app", language="Java", snapshots=[(_iso(7), 100), (_iso(0), 150)])

    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    other_names = [r.full_name for r in _board(boards, "language", "other").rows]
    assert set(other_names) == {"a/kotlin-app", "a/no-lang"}
    for key in LANGUAGES.values():
        rows = _board(boards, "language", key).rows
        expected = ["a/java-app"] if key == "java" else []
        assert [r.full_name for r in rows] == expected


def test_total_board_semantics(conn, topic_table):
    """总星榜：最新快照降序、无增量/窗口概念；单快照新项目照上；as_of 之后的快照不算。"""
    _add_repo(conn, "a/big", language="Python", snapshots=[(_iso(7), 800), (_iso(0), 1000)])
    _add_repo(conn, "a/newbie", language="Python", snapshots=[(_iso(1), 600)])  # 首周缺席口径不影响总星榜
    _add_repo(conn, "a/small", language="Python", snapshots=[(_iso(7), 100), (_iso(0), 200)])
    _add_repo(conn, "a/future", language="Python", snapshots=[("2026-08-10T00:00:00Z", 9999)])  # as_of 之后

    rows = _board(compute_boards(conn, topic_table, period="total", as_of=AS_OF), "language", "python").rows
    assert [r.full_name for r in rows] == ["a/big", "a/newbie", "a/small"]
    assert rows[0].stars == 1000
    assert all(r.delta is None and r.window_days is None for r in rows)


def test_quarter_window(conn, topic_table):
    """季榜同口径、窗口 90 天（滑动 86~94 天）。"""
    repo_q90 = _add_repo(conn, "a/q90", snapshots=[(_iso(90), 100), (_iso(0), 500)])
    repo_q86 = _add_repo(conn, "a/q86", snapshots=[(_iso(86), 100), (_iso(0), 400)])
    repo_q94 = _add_repo(conn, "a/q94", snapshots=[(_iso(94), 100), (_iso(0), 300)])
    repo_q85 = _add_repo(conn, "a/q85", snapshots=[(_iso(85), 100), (_iso(0), 200)])

    deltas = compute_repo_deltas(conn, period="quarter", as_of=AS_OF)
    assert deltas[repo_q90].delta == 400 and deltas[repo_q90].window_days == 90.0
    assert deltas[repo_q86].window_days == 86.0
    assert deltas[repo_q94].window_days == 94.0
    assert deltas[repo_q85].absent_reason == ABSENT_FIRST_WEEK


def test_boards_structure_and_order(conn, topic_table):
    """17 张榜齐备、顺序固定：语言 7 张（LANGUAGES 序+其它收尾）后接主题 10 张（词表序+其他收尾）。"""
    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    assert len(boards) == 17
    assert [(b.kind, b.key) for b in boards[:7]] == [("language", k) for k in (*LANGUAGES.values(), "other")]
    assert [(b.kind, b.key) for b in boards[7:]] == [("topic", k) for k in (*topic_table, "other")]
    assert boards[6].label == "其它语言" and boards[-1].label == "其他"
    # 空库/全缺席时榜仍齐备、行为空列表（页面空态靠它，不抛异常）
    assert all(isinstance(b.rows, list) for b in boards)


def test_full_keys_badge_count_equals_full_rows(conn, topic_table):
    """T-026 单榜整页徽标恒等（k3 初审 F3-6）：full_keys 模式非当前榜 count=min(出席数, top_n)，
    与全量模式该榜 len(rows) 恒等（>top_n 截断两模式同口径）；非当前榜 rows 恒空、当前榜全量构建
    （count 恒 None，页面徽标走 len(rows)），两模式徽标不因单榜化漂移。

    T-028：默认 top_n 已改 50，本测显式 top_n=30 钉截断语义，与默认值脱钩。
    """
    for i in range(35):  # >top_n=30：截断语义两模式对齐
        _add_repo(conn, f"a/py{i:02d}", language="Python", snapshots=[(_iso(7), 100), (_iso(0), 200)])
    for i in range(3):
        _add_repo(conn, f"a/go{i}", language="Go", snapshots=[(_iso(7), 100), (_iso(0), 200)])

    single = compute_boards(
        conn, topic_table, period="week", as_of=AS_OF, top_n=30, full_keys={"language-python"}
    )
    full = compute_boards(conn, topic_table, period="week", as_of=AS_OF, top_n=30)

    py_single, py_full = _board(single, "language", "python"), _board(full, "language", "python")
    assert py_single.count is None  # 当前榜全量构建：count 恒 None（全量语义）
    assert len(py_single.rows) == len(py_full.rows) == 30  # _top 截断 top_n=30
    assert len(py_single.rows) == min(35, 30)

    go_single, go_full = _board(single, "language", "go"), _board(full, "language", "go")
    assert go_single.rows == []  # 非当前榜不建行对象（full_keys 语义）
    assert go_single.count == len(go_full.rows) == 3 == min(3, 30)  # 徽标恒等

    java_single, java_full = _board(single, "language", "java"), _board(full, "language", "java")
    assert java_single.rows == [] and java_single.count == 0  # 空榜：count=0 ≡ 全量 rows=[]
    assert len(java_full.rows) == 0

    assert py_single.rising_rows == [] and go_single.rising_rows == []  # 全库无缺席仓 → 新区恒空（两模式一致）


def test_repo_without_snapshots_not_in_deltas(conn, topic_table):
    """零快照仓库不进 deltas（docstring 承诺锁定）：无总星数可展示，任何榜都安放不了。"""
    repo_empty = _add_repo(conn, "a/empty", language="Go")  # 无快照
    _add_repo(conn, "a/normal", language="Go", snapshots=[(_iso(7), 100), (_iso(0), 200)])

    assert repo_empty not in compute_repo_deltas(conn, period="week", as_of=AS_OF)
    assert repo_empty not in compute_repo_deltas(conn, period="total", as_of=AS_OF)
    go_rows = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "go").rows
    assert [r.full_name for r in go_rows] == ["a/normal"]


def test_quarter_window_wide_side_rejected(conn):
    """季榜宽侧 >94 天同样缺席（周榜两侧已有覆盖，季榜补宽侧）。"""
    repo_q95 = _add_repo(conn, "a/q95", snapshots=[(_iso(95), 100), (_iso(0), 500)])
    assert compute_repo_deltas(conn, period="quarter", as_of=AS_OF)[repo_q95].absent_reason == ABSENT_FIRST_WEEK


def test_future_snapshots_excluded_in_increment(conn):
    """增量口径同样排除 as_of 之后的快照：与 total 共用端点查询，锁定同源行为。"""
    repo = _add_repo(
        conn,
        "a/future-inc",
        language="Rust",
        snapshots=[(_iso(7), 100), (_iso(0), 300), ("2026-08-12T00:00:00Z", 9999)],
    )
    delta = compute_repo_deltas(conn, period="week", as_of=AS_OF)[repo]
    assert delta.stars == 300 and delta.delta == 200  # end 仍是 T-0 快照，不取未来快照


def test_sort_tiebreak_reproducible(conn, topic_table):
    """次序键回归锁：增量相同按星数降序、再按 full_name 升序，同数据重复计算结果恒定。"""
    _add_repo(conn, "a/b-same", language="Java", snapshots=[(_iso(7), 100), (_iso(0), 300)])  # +200
    _add_repo(conn, "a/a-same", language="Java", snapshots=[(_iso(7), 100), (_iso(0), 300)])  # +200
    _add_repo(conn, "a/c-more-stars", language="Java", snapshots=[(_iso(7), 900), (_iso(0), 1100)])  # +200

    rows1 = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "java").rows
    rows2 = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "java").rows
    assert [r.full_name for r in rows1] == ["a/c-more-stars", "a/a-same", "a/b-same"]
    assert [r.full_name for r in rows2] == [r.full_name for r in rows1]


def test_endpoint_sql_uses_index_seek(conn):
    """SQL 红线回归锁：两条端点 SQL 必须是 SEARCH 索引 seek，出现 SCAN 全表扫即变红。"""
    _add_repo(conn, "a/x", snapshots=[(_iso(1), 100)])
    for sql, params in ((_ENDPOINT_SQL, (1, AS_OF)), (_START_AFTER_SQL, (1, AS_OF, AS_OF))):
        plan = [row["detail"] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)]
        assert any("SEARCH" in detail for detail in plan), plan
        assert not any("SCAN" in detail for detail in plan), plan


def test_baseline_sql_uses_index_seek(conn):
    """T-018 新区基线快照 SQL 红线回归锁：主键 ASC LIMIT 1 必须 SEARCH 索引 seek，出现 SCAN 全表扫即变红。"""
    _add_repo(conn, "a/x", snapshots=[(_iso(1), 100)])
    plan = [row["detail"] for row in conn.execute(f"EXPLAIN QUERY PLAN {_BASELINE_SQL}", (1,))]
    assert any("SEARCH" in detail for detail in plan), plan
    assert not any("SCAN" in detail for detail in plan), plan


def test_repo_ids_filter_limits_computation(conn):
    """repo_ids 过滤参数（T-008 评审中-2 转办，T-009 落地）：只算指定集合；dead 剔除口径不变。

    "我的关注"区只对关注 repo_id 集合算增量，不再全池复算；None 默认全池回归（既有行为一字不变）。
    """
    r1 = _add_repo(conn, "a/one", language="Go", snapshots=[(_iso(7), 100), (_iso(0), 200)])
    r2 = _add_repo(conn, "a/two", language="Go", snapshots=[(_iso(7), 100), (_iso(0), 300)])
    r3 = _add_repo(conn, "a/three", language="Go", snapshots=[(_iso(7), 100), (_iso(0), 400)])
    r_dead = _add_repo(conn, "a/dead", dead=1, snapshots=[(_iso(7), 100), (_iso(0), 500)])

    deltas = compute_repo_deltas(conn, period="week", as_of=AS_OF, repo_ids={r1, r2, r_dead})
    assert set(deltas) == {r1, r2}  # 只算指定集；集合里的 dead 照样剔除（调用方按缺席口径补端点星数）
    assert deltas[r1].delta == 100 and deltas[r2].delta == 200

    # 空集 → 空结果：无关注时不打任何端点查询
    assert compute_repo_deltas(conn, period="week", as_of=AS_OF, repo_ids=[]) == {}

    # None 默认全池回归：三个 alive 全在结果里（dead 不在），与未传参数行为一致
    all_deltas = compute_repo_deltas(conn, period="week", as_of=AS_OF)
    assert set(all_deltas) == {r1, r2, r3}
    assert all_deltas[r3].delta == 300
    # total 口径同样接受过滤
    total_deltas = compute_repo_deltas(conn, period="total", as_of=AS_OF, repo_ids=[r3])
    assert set(total_deltas) == {r3} and total_deltas[r3].stars == 400


# ---------- T-018 新崛起区（决策 4 v2）：主榜缺席仓按在池增量 Top 10 分区展示 ----------


def test_rising_admission_and_graduation(conn, topic_table):
    """准入：主榜出席校验失败（跨度不足最小窗口）但有端点快照的仓进新区；
    毕业：跨度满最小窗口当期起出席主榜、自动离开新区（计算口径自然结果，无特判）。"""
    rid = _add_repo(conn, "a/rising", language="Python", snapshots=[(_iso(4), 100), (_iso(0), 130)])
    _add_repo(conn, "a/main", language="Python", snapshots=[(_iso(7), 100), (_iso(0), 300)])

    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    py = _board(boards, "language", "python")
    assert [r.full_name for r in py.rows] == ["a/main"]
    assert [r.full_name for r in py.rising_rows] == ["a/rising"]

    # 毕业：一周后各补一张端点快照 → 两端跨度满最小窗口，出席主榜，新区不再含它们
    next_week = (_AS_OF_DT + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)", (rid, next_week, 200))
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES ((SELECT id FROM repos WHERE full_name = ?), ?, ?)",
        ("a/main", next_week, 350),
    )
    conn.commit()
    boards2 = compute_boards(conn, topic_table, period="week", as_of=next_week)
    py2 = _board(boards2, "language", "python")
    assert {r.full_name for r in py2.rows} == {"a/rising", "a/main"}
    assert py2.rising_rows == []


def test_rising_pool_delta_and_days(conn, topic_table):
    """新区口径：在池增量 = 端点星数 − 入池基线（最旧一张快照）星数；在池天数 = 两端间隔（1 位小数）。

    基线取最旧一张——远超窗口的旧快照不参与主榜候选（S1 跨度 30 天滑出窗口），仍作入池基线参与在池增量；
    F2-1 收窄适配：跨窗空洞（在池天数 > win_max）的缺席老仓不进新区，只有真·新入池（pool_days < win_min）在新区。
    """
    _add_repo(conn, "a/pooled", language="Go", snapshots=[(_iso(30), 50), (_iso(4), 100), (_iso(0), 500)])
    _add_repo(conn, "a/true-new", language="Go", snapshots=[(_iso(4), 100), (_iso(0), 500)])
    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    go = _board(boards, "language", "go")
    assert go.rows == []  # 缺席不进主榜
    (r,) = go.rising_rows
    assert r.full_name == "a/true-new"
    assert r.stars == 500 and r.pool_delta == 400 and r.pool_days == 4.0
    # F2-1 收窄适配：a/pooled 在池 30 天 > win_max（9），跨窗空洞不进新区（两不见）
    assert all(r.full_name != "a/pooled" for b in boards for r in b.rising_rows)

    # 非整日间隔保留 1 位小数（同 window_days 精度）
    _add_repo(conn, "a/pooled-frac", language="Rust", snapshots=[(_iso(2.5), 100), (_iso(0), 300)])
    boards2 = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    (r2,) = _board(boards2, "language", "rust").rising_rows
    assert r2.pool_days == 2.5 and r2.pool_delta == 200


def test_rising_straddle_gap_absent_not_admitted(conn, topic_table):
    """F2-1 收窄：采集停机致跨窗空洞的缺席老仓（在池天数 > win_max）不进新区也不出席主榜（两不见回 v1）。"""
    _add_repo(conn, "a/straddle", language="Go", snapshots=[(_iso(30), 50), (_iso(0), 500)])
    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    go = _board(boards, "language", "go")
    assert go.rows == []  # 跨窗缺席不进主榜（v1 行为）
    assert go.rising_rows == []  # 也不进新区：区头"入池不足 7 天"对在池 30 天仓事实不成立


def test_rising_sort_and_tiebreak(conn, topic_table):
    """新区排序：在池增量降序、负增量沉底；次序键（星数/全名）可复现（与主榜同构）。"""
    _add_repo(conn, "a/r-up", language="Rust", snapshots=[(_iso(3), 100), (_iso(0), 400)])  # +300
    _add_repo(conn, "a/r-more", language="Rust", snapshots=[(_iso(3), 900), (_iso(0), 1100)])  # +200 星数 1100
    _add_repo(conn, "a/r-tie-a", language="Rust", snapshots=[(_iso(3), 100), (_iso(0), 300)])  # +200 星数 300
    _add_repo(conn, "a/r-tie-b", language="Rust", snapshots=[(_iso(3), 100), (_iso(0), 300)])  # +200 星数 300
    _add_repo(conn, "a/r-mid", language="Rust", snapshots=[(_iso(3), 100), (_iso(0), 200)])  # +100
    _add_repo(conn, "a/r-down", language="Rust", snapshots=[(_iso(3), 500), (_iso(0), 450)])  # -50

    rows1 = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "rust").rising_rows
    rows2 = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "rust").rising_rows
    assert [r.full_name for r in rows1] == ["a/r-up", "a/r-more", "a/r-tie-a", "a/r-tie-b", "a/r-mid", "a/r-down"]
    assert [r.full_name for r in rows2] == [r.full_name for r in rows1]


def test_rising_dead_excluded(conn, topic_table):
    """新区 dead 剔除同主榜：dead=1 仓库即使有快照也不进新区。"""
    _add_repo(conn, "a/ghost", language="Java", dead=1, snapshots=[(_iso(3), 100), (_iso(0), 900)])
    _add_repo(conn, "a/alive", language="Java", snapshots=[(_iso(3), 100), (_iso(0), 200)])

    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    java = _board(boards, "language", "java")
    assert [r.full_name for r in java.rising_rows] == ["a/alive"]
    assert all(r.full_name != "a/ghost" for b in boards for r in b.rising_rows)


def test_rising_top_n_capped(conn, topic_table):
    """新区每分类榜 Top 10：11 个缺席仓截掉在池增量最小者；不足按实际。"""
    for i in range(1, 12):
        _add_repo(conn, f"a/ris-{i:02d}", language="Python", snapshots=[(_iso(3), 1000), (_iso(0), 1000 + i)])
    py = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "python")
    assert len(py.rising_rows) == 10
    assert py.rising_rows[0].pool_delta == 11
    assert {r.pool_delta for r in py.rising_rows} == set(range(2, 12))


def test_rising_empty_without_absent(conn, topic_table):
    """无缺席仓 → 所有榜新区恒空（页面"空区不渲染"的数据基础）。"""
    _add_repo(conn, "a/attending", language="Go", snapshots=[(_iso(7), 100), (_iso(0), 300)])
    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    assert all(b.rising_rows == [] for b in boards)


def test_total_period_never_has_rising(conn, topic_table):
    """total 口径无新区（无缺席概念）：单快照新项目照上主榜，rising 恒空。"""
    _add_repo(conn, "a/newbie", language="Python", snapshots=[(_iso(1), 600)])
    boards = compute_boards(conn, topic_table, period="total", as_of=AS_OF)
    assert all(b.rising_rows == [] for b in boards)
    assert [r.full_name for r in _board(boards, "language", "python").rows] == ["a/newbie"]


def test_rising_quarter_period(conn, topic_table):
    """季榜新区同口径：缺席（跨度 <86 天）进新区，在池天数按实际两端间隔。"""
    _add_repo(conn, "a/q-new", language="Python", snapshots=[(_iso(50), 100), (_iso(0), 300)])
    boards = compute_boards(conn, topic_table, period="quarter", as_of=AS_OF)
    py = _board(boards, "language", "python")
    assert py.rows == []
    (r,) = py.rising_rows
    assert r.pool_delta == 200 and r.pool_days == 50.0


def test_rising_repeats_across_boards(conn, topic_table):
    """新区行跨榜重复与主榜同口径：命中多主题的缺席仓在每个命中主题榜新区各出现一次。"""
    _add_repo(conn, "a/ai-rise", topics=["ai", "react"], snapshots=[(_iso(3), 100), (_iso(0), 300)])
    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    assert [r.full_name for r in _board(boards, "topic", "ai").rising_rows] == ["a/ai-rise"]
    assert [r.full_name for r in _board(boards, "topic", "frontend").rising_rows] == ["a/ai-rise"]


# ---------- T-033 新项目区（决策 4：出席 ∧ 创建 < 1 年 ∧ 不在主榜 Top50，Top 20） ----------


def test_fresh_admission_and_null_exclusion(conn, topic_table):
    """准入：出席仓且 github_created_at 非 NULL 且年龄 < 365 天进新项目区；NULL（未回填）一律不进；
    缺席仓不进（归新崛起区，两区互斥）。"""
    # 出席但被 51 个老仓挤出主榜 Top50 → 新项目区候选
    _add_repo(conn, "a/fresh", language="Python", github_created_at="2026-07-01T00:00:00Z",
              snapshots=[(_iso(7), 50), (_iso(0), 400)])  # delta 350
    _add_repo(conn, "a/null", language="Python", snapshots=[(_iso(7), 50), (_iso(0), 300)])  # NULL 未回填
    _add_repo(conn, "a/absent", language="Python", github_created_at="2026-07-01T00:00:00Z",
              snapshots=[(_iso(3), 100), (_iso(0), 300)])  # 缺席 → 新崛起区
    for i in range(51):  # 老仓占满 python 主榜 Top50（delta 1000..1050 > 350）
        _add_repo(conn, f"o/old-{i:02d}", language="Python", github_created_at="2020-01-01T00:00:00Z",
                  snapshots=[(_iso(7), 1000), (_iso(0), 2000 + i)])

    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    py = _board(boards, "language", "python")
    assert [r.full_name for r in py.fresh] == ["a/fresh"]  # NULL 仓不进
    assert [r.full_name for r in py.rising_rows] == ["a/absent"]  # 缺席仓归新崛起区（互斥）
    assert "a/fresh" not in {r.full_name for r in py.rows}
    assert len(py.rows) == 50  # 主榜照常 Top50


def test_fresh_total_period_admission(conn, topic_table):
    """total 口径新项目区：无出席概念，准入 = 创建 < 1 年 ∧ alive ∧ 不在主榜，按总星降序。"""
    _add_repo(conn, "a/t1", language="Python", github_created_at="2026-07-01T00:00:00Z",
              snapshots=[(_iso(1), 1000)])
    _add_repo(conn, "a/t2", language="Python", github_created_at="2026-06-01T00:00:00Z",
              snapshots=[(_iso(1), 900)])
    _add_repo(conn, "a/old", language="Python", github_created_at="2020-01-01T00:00:00Z",
              snapshots=[(_iso(1), 5000)])
    for i in range(51):
        _add_repo(conn, f"o/old-{i:02d}", language="Python", github_created_at="2019-01-01T00:00:00Z",
                  snapshots=[(_iso(1), 6000 + i * 10)])

    py = _board(compute_boards(conn, topic_table, period="total", as_of=AS_OF), "language", "python")
    assert [r.full_name for r in py.fresh] == ["a/t1", "a/t2"]  # 总星降序；a/old 满 1 年不进；主榜被老仓占满
    assert [r.full_name for r in py.rows][0].startswith("o/old-")
    assert all(r.delta is None for r in py.fresh)  # total 无增量概念（行形态同主榜）


def test_fresh_365_day_boundary(conn, topic_table):
    """365 天边界：as_of − github_created_at 差 364 天进、365 天整不进（.days 下取整，< 365 严格语义）。"""
    _add_repo(conn, "a/b364", language="Go", github_created_at="2025-08-10T00:00:00Z",
              snapshots=[(_iso(7), 50), (_iso(0), 400)])  # 2026-08-09 − 2025-08-10 = 364 天
    _add_repo(conn, "a/b365", language="Go", github_created_at="2025-08-09T00:00:00Z",
              snapshots=[(_iso(7), 50), (_iso(0), 300)])  # = 365 天整：不进
    _add_repo(conn, "a/old", language="Go", github_created_at="2010-01-01T00:00:00Z",
              snapshots=[(_iso(7), 50), (_iso(0), 200)])  # 远老仓：不进
    for i in range(51):
        _add_repo(conn, f"o/old-{i:02d}", language="Go", github_created_at="2010-01-01T00:00:00Z",
                  snapshots=[(_iso(7), 1000), (_iso(0), 2000 + i)])

    go = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "go")
    assert [r.full_name for r in go.fresh] == ["a/b364"]  # 365 天整与远老仓都不进


def test_fresh_excludes_main_board_and_no_double_count(conn, topic_table):
    """与主榜去重不重影：进主榜 Top50 的仓不在新项目区；被挤出主榜的才进；同仓同榜只出现一次。"""
    _add_repo(conn, "a/main", language="Rust", github_created_at="2026-07-01T00:00:00Z",
              snapshots=[(_iso(7), 100), (_iso(0), 2500)])  # delta 2400：凭增量进主榜
    _add_repo(conn, "a/edge", language="Rust", github_created_at="2026-07-01T00:00:00Z",
              snapshots=[(_iso(7), 100), (_iso(0), 300)])  # delta 200：被 51 个老仓挤出主榜
    for i in range(51):
        _add_repo(conn, f"o/old-{i:02d}", language="Rust", github_created_at="2019-01-01T00:00:00Z",
                  snapshots=[(_iso(7), 1000), (_iso(0), 2000 + i)])

    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    rust = _board(boards, "language", "rust")
    main_names = {r.full_name for r in rust.rows}
    fresh_names = {r.full_name for r in rust.fresh}
    assert main_names & fresh_names == set()  # 不重影
    assert "a/main" in main_names and "a/main" not in fresh_names  # 凭增量进主榜 → 不在新区
    assert "a/edge" in fresh_names and "a/edge" not in main_names  # 挤出主榜 → 新区
    assert len(main_names | fresh_names) == len(main_names) + len(fresh_names)  # 全集互斥


def test_fresh_top_20_capped_and_no_padding(conn, topic_table):
    """Top 20 截断与不补位：22 个候选截掉排序最后 2 个；不足按实际（无凑数行）。"""
    for i in range(22):
        _add_repo(conn, f"a/f-{i:02d}", language="Python", github_created_at="2026-01-01T00:00:00Z",
                  snapshots=[(_iso(7), 1000), (_iso(0), 1000 + i * 10)])  # delta 0..210
    for i in range(51):
        _add_repo(conn, f"o/old-{i:02d}", language="Python", github_created_at="2019-01-01T00:00:00Z",
                  snapshots=[(_iso(7), 1000), (_iso(0), 2000 + i)])

    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    py = _board(boards, "language", "python")
    assert len(py.fresh) == 20  # 22 个候选截 Top 20
    assert py.fresh[0].full_name == "a/f-21"  # 增量最大者居首
    assert {r.full_name for r in py.fresh} == {f"a/f-{i:02d}" for i in range(2, 22)}  # delta 0/10 的被截掉

    # 不足 20 按实际：Java 榜 3 个候选（+51 占位挤出主榜）→ 只展示 3 行，无凑数
    for i in range(3):
        _add_repo(conn, f"j/f-{i}", language="Java", github_created_at="2026-01-01T00:00:00Z",
                  snapshots=[(_iso(7), 2000), (_iso(0), 2000 + i * 10)])  # delta 0/10/20
    for i in range(51):
        _add_repo(conn, f"j/old-{i:02d}", language="Java", github_created_at="2019-01-01T00:00:00Z",
                  snapshots=[(_iso(7), 1000), (_iso(0), 2000 + i)])
    java = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "java")
    assert len(java.fresh) == 3
    assert [r.full_name for r in java.fresh] == ["j/f-2", "j/f-1", "j/f-0"]  # delta 降序（0 也列）


def test_three_zone_flow_mutual_exclusion(conn, topic_table):
    """三区流转互斥（规格 Scenario）：新仓入池第 3 天（缺席）只在新崛起区；满窗口出席但增量进不了
    主榜 Top50 → 只在新项目区；增量涨进主榜 Top50 → 只在主榜——同仓同榜三区不重影。"""
    rid = _add_repo(conn, "a/fresh", language="Python", github_created_at="2026-07-01T00:00:00Z",
                    snapshots=[(_iso(3), 100), (_iso(0), 400)])
    py = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "python")
    assert [r.full_name for r in py.rising_rows] == ["a/fresh"]  # 缺席 → 新崛起区
    assert py.fresh == [] and py.rows == []

    # 补 7 天前快照 → 满窗口出席（delta 350）；51 个老仓（delta 1000..1050）占满主榜 Top50
    conn.execute("INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)", (rid, _iso(7), 50))
    for i in range(51):
        _add_repo(conn, f"o/old-{i:02d}", language="Python", github_created_at="2019-01-01T00:00:00Z",
                  snapshots=[(_iso(7), 1000), (_iso(0), 2000 + i)])
    py2 = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "python")
    assert "a/fresh" not in {r.full_name for r in py2.rows}  # 挤不进主榜
    assert [r.full_name for r in py2.fresh] == ["a/fresh"]  # 出席且未满 1 年 → 新项目区
    assert py2.rising_rows == []  # 毕业离开新崛起区

    # 端点星数拉满 → 增量 1050 并列前茅：进主榜 → 从新项目区消失
    conn.execute("UPDATE star_snapshots SET stars = 1100 WHERE repo_id = ? AND captured_at = ?", (rid, _iso(0)))
    py3 = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "python")
    assert "a/fresh" in {r.full_name for r in py3.rows}
    assert "a/fresh" not in {r.full_name for r in py3.fresh}
    assert py3.rising_rows == []


def test_fresh_full_keys_single_board_only(conn, topic_table):
    """T-033 决策 6：full_keys 单榜模式仅指定榜算 fresh——当前榜 fresh 正常、非当前榜 fresh 恒空列表
    （不计数）；全量模式各榜 fresh 就位。"""
    _add_repo(conn, "a/py-fresh", language="Python", github_created_at="2026-07-01T00:00:00Z",
              snapshots=[(_iso(7), 50), (_iso(0), 400)])
    _add_repo(conn, "a/go-fresh", language="Go", github_created_at="2026-07-01T00:00:00Z",
              snapshots=[(_iso(7), 50), (_iso(0), 300)])
    for i in range(51):
        _add_repo(conn, f"o/old-{i:02d}", language="Python", github_created_at="2019-01-01T00:00:00Z",
                  snapshots=[(_iso(7), 1000), (_iso(0), 2000 + i)])
        _add_repo(conn, f"g/old-{i:02d}", language="Go", github_created_at="2019-01-01T00:00:00Z",
                  snapshots=[(_iso(7), 1000), (_iso(0), 2000 + i)])

    single = compute_boards(conn, topic_table, period="week", as_of=AS_OF, full_keys={"language-python"})
    py, go = _board(single, "language", "python"), _board(single, "language", "go")
    assert [r.full_name for r in py.fresh] == ["a/py-fresh"]  # 指定榜 fresh 全量
    assert go.fresh == []  # 非指定榜 fresh 恒空列表（不计数）
    assert go.count == 50  # 非指定榜只归桶计数（Go 主榜行数）

    full = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    assert [r.full_name for r in _board(full, "language", "python").fresh] == ["a/py-fresh"]
    assert [r.full_name for r in _board(full, "language", "go").fresh] == ["a/go-fresh"]


def test_rows_carry_created_year(conn, topic_table):
    """T-033：主榜行/新项目区行/新崛起区行统一携带 created_year（github_created_at 前 4 位）；
    NULL 未回填 → None（模板不渲染标注的数据基础）。"""
    _add_repo(conn, "a/fresh", language="Python", github_created_at="2026-07-01T00:00:00Z",
              snapshots=[(_iso(7), 50), (_iso(0), 400)])
    _add_repo(conn, "a/rising", language="Rust", github_created_at="2026-06-01T00:00:00Z",
              snapshots=[(_iso(3), 100), (_iso(0), 300)])
    _add_repo(conn, "a/no-created", language="Go", snapshots=[(_iso(7), 100), (_iso(0), 200)])
    for i in range(51):
        _add_repo(conn, f"o/old-{i:02d}", language="Python", github_created_at="2019-01-01T00:00:00Z",
                  snapshots=[(_iso(7), 1000), (_iso(0), 2000 + i)])

    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    py = _board(boards, "language", "python")
    assert py.fresh[0].created_year == 2026  # 新项目区行
    assert {r.created_year for r in py.rows} == {2019}  # 主榜行
    rust = _board(boards, "language", "rust")
    assert rust.rising_rows[0].created_year == 2026  # 新崛起区行
    go = _board(boards, "language", "go")
    assert go.rows[0].created_year is None  # NULL 未回填 → None


# ---------- T-020 端点快照日期标注：行结构透传 captured_at（纯展示数据源，一行计算逻辑不动） ----------


def test_rows_carry_endpoint_captured_at(conn, topic_table):
    """主榜行/新区行携带端点快照时间（透传 RepoDelta.captured_at）；total 口径同样有值——页面端点日期标注的数据基础。

    值 = 最新快照的 captured_at 原样（出席/缺席都有值，T-018 起语义统一）；切片形态在视图层（routes._endpoint_note）锁。
    """
    _add_repo(conn, "a/main", language="Go", snapshots=[(_iso(7), 100), (_iso(0), 300)])  # 出席主榜
    _add_repo(conn, "a/rise", language="Go", snapshots=[(_iso(3), 100), (_iso(0), 500)])  # 缺席进新区

    go = _board(compute_boards(conn, topic_table, period="week", as_of=AS_OF), "language", "go")
    (main,) = go.rows
    assert main.captured_at == _iso(0)  # 端点 = 最新快照时间
    (rising,) = go.rising_rows
    assert rising.captured_at == _iso(0)  # 新区行同样携带

    # total 口径：无缺席概念，主榜行同样携带端点时间
    tgo = _board(compute_boards(conn, topic_table, period="total", as_of=AS_OF), "language", "go")
    assert [r.captured_at for r in tgo.rows] == [_iso(0), _iso(0)]
