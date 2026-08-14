"""T-027 榜单预计算缓存测试：序列化 round-trip、三条降级判定、precompute 落表、页面直读/降级、daily_job 接线。

与 test_report.py / test_web.py / test_jobs.py 同口径：tmp_path 真实文件库（WAL 对 :memory: 不生效）；
页面走 TestClient（lifespan 跑 init_db，RADAR_JOBS_ENABLED=0）；jobs 走 asyncio.run + 假 client
（不引 pytest-asyncio）。既有测试对本次改动透明：测试库 board_cache 为空 → 页面自动走实时路径。
"""

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.classify import load_topics
from app.collector.discover import DailyStats
from app.collector.github import utc_now_iso
from app.config import BASE_DIR
from app.db import get_conn, init_db
from app.jobs import daily_job
from app.main import app
from app.report import (
    PERIODS,
    compute_boards,
    load_board_cache,
    precompute_boards,
    save_board_cache,
    week_label,
)

SNAP_DAY = "2026-08-09"  # 固定历史日期（同 test_web）：2026-W32 周日
WEEK_LABEL = "2026-W32"
TOPICS_PATH = BASE_DIR / "config" / "topics.yaml"
AS_OF = "2026-08-09T00:00:00Z"  # 单元级固定基准时刻（同 test_report）：窗口语义与"今天"解耦
NOW = datetime.now(timezone.utc)  # 页面级用例的基准"此刻"：快照相对它造，任意运行日期时间稳健

_META_GENERATED_RE = re.compile(r"页面渲染于 \d{4}-\d{2}-\d{2} \d{2}:\d{2}")


def _iso_ago(days=0, hours=0):
    """NOW 之前指定偏移的定长 UTC 时间戳（页面级用例造快照用）。"""
    return (NOW - timedelta(days=days, hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_repo(conn, name, *, language=None, topics=(), description_en=None, dead=0, snapshots=()):
    """插一个仓库及其快照，返回 repo_id（同 test_web 形态）。"""
    created_at = min((ts for ts, _ in snapshots), default=SNAP_DAY)
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, language, topics, dead, source, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'test', ?)",
        (name, f"node-{name}", description_en, language, json.dumps(list(topics)), dead, created_at),
    )
    repo_id = cur.lastrowid
    for captured_at, stars in snapshots:
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, captured_at, stars),
        )
    return repo_id


def _seed_roundtrip(conn):
    """round-trip / 降级判定种子：出席行＋新区行＋None 字段行＋dead 剔除，覆盖 payload 全字段形态。"""
    _add_repo(
        conn,
        "a/attend",
        language="Python",
        topics=["ai"],
        description_en="desc",
        snapshots=[("2026-08-02T00:00:00Z", 300), (f"{SNAP_DAY}T00:00:00Z", 400)],
    )
    _add_repo(conn, "a/rising", language="Rust", snapshots=[("2026-08-06T00:00:00Z", 100), (f"{SNAP_DAY}T00:00:00Z", 400)])
    _add_repo(conn, "a/no-lang", snapshots=[("2026-08-02T00:00:00Z", 100), (f"{SNAP_DAY}T00:00:00Z", 200)])
    _add_repo(conn, "a/dead", language="Go", dead=1, snapshots=[("2026-08-02T00:00:00Z", 100), (f"{SNAP_DAY}T00:00:00Z", 900)])


def _seed_cache_data(conn):
    """当前周活跃种子（快照相对 NOW 造）：周窗口恒出席/恒新区/恒 None 字段，任意运行日期时间稳健。"""
    _add_repo(
        conn,
        "a/attend",
        language="Python",
        topics=["ai"],
        description_en="desc",
        snapshots=[(_iso_ago(days=7), 300), (_iso_ago(hours=1), 400)],
    )
    _add_repo(conn, "a/rising", language="Rust", snapshots=[(_iso_ago(days=3), 100), (_iso_ago(hours=1), 400)])
    _add_repo(conn, "a/no-lang", snapshots=[(_iso_ago(days=7), 100), (_iso_ago(hours=1), 200)])


def _seed_history(conn):
    """历史周种子（固定快照）：2026-W32 期末窗口恒出席，as_of 固定与"今天"解耦。"""
    _add_repo(conn, "a/py", language="Python", topics=["ai"], snapshots=[("2026-08-02T00:00:00Z", 300), (f"{SNAP_DAY}T00:00:00Z", 400)])


def _seed_straddle(conn):
    """跨窗空洞缺席仓（F2-1 收窄语义）：主榜缺席且不进新区——主榜＋新区全空的降级触发种子。"""
    _add_repo(conn, "a/straddle", language="Python", snapshots=[("2026-05-01T00:00:00Z", 50), ("2026-06-15T00:00:00Z", 300)])


def _assert_boards_equal(a, b):
    """逐字段全等：Board 元字段＋主榜行/新区行所有 dataclass 字段；count 均恒 None（缓存不存 count）。"""
    assert a is not None and b is not None
    assert len(a) == len(b)
    for ba, bb in zip(a, b):
        assert (ba.kind, ba.key, ba.label) == (bb.kind, bb.key, bb.label)
        assert ba.count is None and bb.count is None
        assert [vars(r) for r in ba.rows] == [vars(r) for r in bb.rows]
        assert [vars(r) for r in ba.rising_rows] == [vars(r) for r in bb.rising_rows]


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


# ===== 序列化 round-trip：compute_boards → save → load 逐字段还原 =====


def test_round_trip_preserves_all_fields(conn, topic_table):
    """三口径 round-trip 逐字段相等：主榜行（含 None 字段/负 delta 不在本种子，但 None 全覆盖）＋新区行；
    Board.count 还原恒 None（不存不补）。"""
    _seed_roundtrip(conn)
    conn.commit()
    for period in PERIODS:
        real = compute_boards(conn, topic_table, period=period, as_of=AS_OF)
        assert len(real) == 17  # 全量 17 榜
        labels = {"week": week_label(date.fromisoformat(AS_OF[:10])), "quarter": "2026-Q3", "total": "all"}
        save_board_cache(conn, period=period, label=labels[period], as_of=AS_OF, boards=real)
        conn.commit()
        loaded = load_board_cache(conn, period=period, label=labels[period])
        _assert_boards_equal(loaded, real)
        # 抽查关键形态：新区行确实进了 payload（还原非空）
        if period == "week":
            assert any(b.rising_rows for b in loaded)  # a/rising 进新区
    # None 字段还原抽查走 total 口径（无缺席概念，a/no-lang 恒在主榜；周/季口径它缺席不进主榜）
    total_loaded = load_board_cache(conn, period="total", label="all")
    assert any(r.language is None for b in total_loaded for r in b.rows)  # a/no-lang 的 None 字段还原


def test_round_trip_is_idempotent(conn, topic_table):
    """同日重跑 save（INSERT OR REPLACE）：行数不长、内容为新值。"""
    _seed_roundtrip(conn)
    conn.commit()
    real = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    save_board_cache(conn, period="week", label="2026-W32", as_of=AS_OF, boards=real)
    conn.commit()
    save_board_cache(conn, period="week", label="2026-W32", as_of=AS_OF, boards=real)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM board_cache WHERE period = 'week'").fetchone()[0] == 1
    assert load_board_cache(conn, period="week", label="2026-W32") is not None


# ===== load 三条降级判定：任一不过返回 None（调用方走实时算路径） =====


def test_load_none_when_no_row(conn, topic_table):
    """判定①：表无该 period 行（未预计算/预计算失败）→ None。"""
    assert load_board_cache(conn, period="week", label="2026-W32") is None
    assert load_board_cache(conn, period="total", label="all") is None


def test_load_none_when_label_mismatch(conn, topic_table):
    """判定②：缓存 label 与请求 label 不符（跨周/跨季凌晨窗口）→ None；相符则命中。"""
    _seed_roundtrip(conn)
    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    save_board_cache(conn, period="week", label="2026-W31", as_of=AS_OF, boards=boards)
    conn.commit()
    assert load_board_cache(conn, period="week", label="2026-W32") is None
    assert load_board_cache(conn, period="week", label="2026-W31") is not None


def test_load_none_when_new_snapshot_after_as_of(conn, topic_table):
    """判定③：库内 MAX(captured_at) 晚于缓存 as_of（采集写了新快照但预计算未跑/失败）→ None。"""
    _seed_roundtrip(conn)
    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    save_board_cache(conn, period="week", label="2026-W32", as_of=AS_OF, boards=boards)
    conn.commit()
    assert load_board_cache(conn, period="week", label="2026-W32") is not None  # 基线命中
    repo_id = conn.execute("SELECT id FROM repos WHERE full_name = 'a/attend'").fetchone()["id"]
    conn.execute("INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, '2026-08-10T00:00:00Z', 500)", (repo_id,))
    conn.commit()
    assert load_board_cache(conn, period="week", label="2026-W32") is None


def test_load_not_invalidated_by_snapshot_before_as_of(conn, topic_table):
    """判定③边界：新增快照不晚于缓存 as_of（同日补跑快照，预计算尚未覆盖）→ 缓存仍有效。"""
    _seed_roundtrip(conn)
    boards = compute_boards(conn, topic_table, period="week", as_of=AS_OF)
    save_board_cache(conn, period="week", label="2026-W32", as_of="2026-08-09T23:59:59Z", boards=boards)
    conn.commit()
    repo_id = conn.execute("SELECT id FROM repos WHERE full_name = 'a/attend'").fetchone()["id"]
    conn.execute("INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, '2026-08-09T12:00:00Z', 450)", (repo_id,))
    conn.commit()
    assert load_board_cache(conn, period="week", label="2026-W32") is not None  # MAX=12:00 < 23:59:59 → 有效


# ===== precompute_boards：三口径全量一次落表，label 按 as_of 换算 =====


def test_precompute_writes_three_rows_with_labels(conn, topic_table):
    """precompute 写 3 行（week/quarter/total），label 换算正确（2026-W32/2026-Q3/'all'），as_of 三行一致；
    摘要返回每口径榜数/行数。"""
    _seed_roundtrip(conn)
    conn.commit()
    summary = precompute_boards(conn, topic_table, as_of=AS_OF)
    rows = conn.execute("SELECT period, label, as_of, computed_at FROM board_cache ORDER BY period").fetchall()
    assert [(r["period"], r["label"]) for r in rows] == [("quarter", "2026-Q3"), ("total", "all"), ("week", "2026-W32")]
    assert all(r["as_of"] == AS_OF for r in rows)
    assert all(r["computed_at"] for r in rows)
    assert summary == {
        # 跨榜重复计数（命中多主题/多榜各算一行）：a/attend 出席 2 行（python 语言榜＋ai 主题榜）、
        # a/no-lang 2 行（other 语言榜＋other 主题榜）；a/rising 缺席进新区 2 行
        "week": {"boards": 17, "rows": 4, "rising": 2},
        # 快照跨度 7 天 < 86：季窗口全缺席，三个仓都进新区（各 2 榜）→ 主榜 0 行
        "quarter": {"boards": 17, "rows": 0, "rising": 6},
        "total": {"boards": 17, "rows": 6, "rising": 0},
    }
    # 落表内容与同 as_of 实时算逐字段一致
    _assert_boards_equal(load_board_cache(conn, period="week", label="2026-W32"), compute_boards(conn, topic_table, period="week", as_of=AS_OF))
    # 幂等：重复跑不涨行数
    precompute_boards(conn, topic_table, as_of=AS_OF)
    assert conn.execute("SELECT COUNT(*) FROM board_cache").fetchone()[0] == 3


# ===== 页面级集成：直读缓存 / 删缓存降级 / 历史期次不读缓存 =====


def _make_client(tmp_path, monkeypatch, seed):
    db = tmp_path / "web.db"
    init_db(db)
    conn = get_conn(db)
    seed(conn)
    conn.commit()
    conn.close()
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    return TestClient(app), db


def _strip_generated(html: str) -> str:
    """剔除"页面渲染于"时刻行（分钟粒度，两次请求跨分钟属预期差异，比较前归一）。"""
    return _META_GENERATED_RE.sub("页面渲染于 <时刻>", html)


def test_page_week_cache_hit_content_identical_and_not_stale(topic_table, tmp_path, monkeypatch):
    """有缓存时页面 200 且榜单行内容与无缓存实时算一致（剔除渲染时刻行）；
    改库星数后页面仍显示缓存旧值 → 证明真的读了缓存（非实时算）。"""
    client, db = _make_client(tmp_path, monkeypatch, _seed_cache_data)
    conn = get_conn(db)
    try:
        with client:
            real = client.get("/?board=all").text
            now_iso = utc_now_iso()
            boards = compute_boards(conn, topic_table, period="week", as_of=now_iso)
            save_board_cache(conn, period="week", label=week_label(date.fromisoformat(now_iso[:10])), as_of=now_iso, boards=boards)
            conn.commit()
            # 改库星数（不动 captured_at）：读缓存 → 显示缓存旧值；实时算 → 显示新值 100.0k
            conn.execute(
                "UPDATE star_snapshots SET stars = 99999 WHERE repo_id = (SELECT id FROM repos WHERE full_name = 'a/attend')"
            )
            conn.commit()
            cached = client.get("/?board=all").text
            assert _strip_generated(real) == _strip_generated(cached)  # 内容与无缓存实时算一致
            assert "a/attend" in cached
            assert "100.0k" not in cached  # 缓存命中证据：改库后星数未反映
    finally:
        conn.close()


def test_page_total_cache_hit(topic_table, tmp_path, monkeypatch):
    """total 页恒读缓存（label='all'）；改库后页面显示缓存旧值。"""
    client, db = _make_client(tmp_path, monkeypatch, _seed_cache_data)
    conn = get_conn(db)
    try:
        with client:
            now_iso = utc_now_iso()
            total = compute_boards(conn, topic_table, period="total", as_of=now_iso)
            save_board_cache(conn, period="total", label="all", as_of=now_iso, boards=total)
            conn.commit()
            conn.execute(
                "UPDATE star_snapshots SET stars = 99999 WHERE repo_id = (SELECT id FROM repos WHERE full_name = 'a/attend')"
            )
            conn.commit()
            text = client.get("/total?board=all").text
            assert "100.0k" not in text  # 读缓存
            assert "a/attend" in text
    finally:
        conn.close()


def test_page_fallback_when_cache_missing(topic_table, tmp_path, monkeypatch):
    """删掉缓存行后页面仍 200（降级不白屏）：走实时算路径，改库星数如实反映。"""
    client, db = _make_client(tmp_path, monkeypatch, _seed_cache_data)
    conn = get_conn(db)
    try:
        with client:
            now_iso = utc_now_iso()
            boards = compute_boards(conn, topic_table, period="week", as_of=now_iso)
            save_board_cache(conn, period="week", label=week_label(date.fromisoformat(now_iso[:10])), as_of=now_iso, boards=boards)
            conn.commit()
            conn.execute("DELETE FROM board_cache")
            conn.execute(
                "UPDATE star_snapshots SET stars = 99999 WHERE repo_id = (SELECT id FROM repos WHERE full_name = 'a/attend')"
            )
            conn.commit()
            text = client.get("/?board=all").text
            assert "100.0k" in text  # 无缓存 → 实时算（改库值反映，不白屏）
            assert "a/attend" in text
    finally:
        conn.close()


def test_history_week_never_reads_cache(tmp_path, monkeypatch):
    """历史周次（?week=2026-W32）不读缓存：预置脏缓存行（label 相符、as_of 远未来、payload 空榜）
    若被读会得到空榜并触发降级——页面仍显示实时算内容即证明未查表。"""
    client, db = _make_client(tmp_path, monkeypatch, _seed_history)
    with client:
        conn = get_conn(db)
        conn.execute(
            "INSERT INTO board_cache (period, label, as_of, payload, computed_at)"
            " VALUES ('week', '2026-W32', '2099-01-01T00:00:00Z', '[]', '2099-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()
        text = client.get(f"/?week={WEEK_LABEL}&board=all").text
        assert "a/py" in text  # 实时算内容（历史周不查表）
        assert "暂无可展示数据" not in text  # 未读脏缓存空榜 → 未触发"无数据"降级


def test_current_week_cache_label_mismatch_falls_back_to_live(topic_table, tmp_path, monkeypatch):
    """当前周缓存 label 不符（跨周凌晨窗口：缓存还是上周标签）→ load 返回 None → 页面实时算（改库值反映）。"""
    client, db = _make_client(tmp_path, monkeypatch, _seed_cache_data)
    conn = get_conn(db)
    try:
        with client:
            now_iso = utc_now_iso()
            boards = compute_boards(conn, topic_table, period="week", as_of=now_iso)
            last_week_label = week_label(date.fromisoformat(now_iso[:10]) - timedelta(days=7))
            save_board_cache(conn, period="week", label=last_week_label, as_of=now_iso, boards=boards)  # 存上周标签
            conn.commit()
            conn.execute(
                "UPDATE star_snapshots SET stars = 99999 WHERE repo_id = (SELECT id FROM repos WHERE full_name = 'a/attend')"
            )
            conn.commit()
            text = client.get("/?board=all").text
            assert "100.0k" in text  # 缓存 label 不符 → 实时算
    finally:
        conn.close()


def test_single_board_cache_hit_and_stale_cache_fallback(topic_table, tmp_path, monkeypatch):
    """单榜模式页面级两态（k3 评审 F2-1 修复锁＋F3-2(b) 单榜缓存命中行为锁）：

    1) 完整缓存时单榜 URL 直读缓存（改库星数不反映 → 证明命中）；
    2) 词表变更窗口期的旧缓存 payload 缺当前白名单放行的榜 key → 单榜 URL 不 500（StopIteration），
       置 None 降级实时算（改库星数如实反映）。
    """
    client, db = _make_client(tmp_path, monkeypatch, _seed_cache_data)
    conn = get_conn(db)
    try:
        with client:
            now_iso = utc_now_iso()
            label = week_label(date.fromisoformat(now_iso[:10]))
            boards = compute_boards(conn, topic_table, period="week", as_of=now_iso)
            save_board_cache(conn, period="week", label=label, as_of=now_iso, boards=boards)
            conn.commit()
            conn.execute(
                "UPDATE star_snapshots SET stars = 99999 WHERE repo_id = (SELECT id FROM repos WHERE full_name = 'a/attend')"
            )
            conn.commit()
            hit = client.get("/?board=language-python")
            assert hit.status_code == 200
            assert "a/attend" in hit.text
            assert "100.0k" not in hit.text  # 单榜缓存命中：显示缓存旧值
            # 模拟旧词表缓存：payload 摘掉 language-python 榜（该 key 仍在当前白名单）
            stale = [b for b in boards if not (b.kind == "language" and b.key == "python")]
            save_board_cache(conn, period="week", label=label, as_of=now_iso, boards=stale)
            conn.commit()
            miss = client.get("/?board=language-python")
            assert miss.status_code == 200  # 修复前：next(...) StopIteration → 500
            assert "100.0k" in miss.text  # 缓存答不了 → 实时算（改库值反映）
    finally:
        conn.close()


def test_fallback_current_week_reads_total_cache(topic_table, tmp_path, monkeypatch):
    """首期空态降级：当前周主榜＋新区全空 → 先读 total 缓存（页面显示缓存总星内容）；删缓存后实时算仍 200。"""
    client, db = _make_client(tmp_path, monkeypatch, _seed_straddle)
    conn = get_conn(db)
    try:
        with client:
            now_iso = utc_now_iso()
            total = compute_boards(conn, topic_table, period="total", as_of=now_iso)
            save_board_cache(conn, period="total", label="all", as_of=now_iso, boards=total)
            conn.commit()
            # 改库星数：读 total 缓存 → 显示缓存旧值 300；实时算 → 新值 99999 → "100.0k"
            conn.execute(
                "UPDATE star_snapshots SET stars = 99999 WHERE repo_id = (SELECT id FROM repos WHERE full_name = 'a/straddle')"
            )
            conn.commit()
            text = client.get("/").text
            assert 'class="notice"' in text  # 降级页
            assert "a/straddle" in text
            assert "100.0k" not in text  # 降级内容来自 total 缓存
            # 删缓存 → 实时降级仍 200 且内容一致（不白屏）
            conn.execute("DELETE FROM board_cache")
            conn.commit()
            text2 = client.get("/").text
            assert 'class="notice"' in text2
            assert "a/straddle" in text2
            assert "100.0k" in text2  # 实时算降级（改库值反映）
    finally:
        conn.close()


def test_fallback_history_week_ignores_total_cache(tmp_path, monkeypatch):
    """历史期次降级不读 total 缓存：脏 total 缓存（空榜）若被读 → 降级页空"暂无可展示数据"；
    实际按请求 as_of 实时算 → 历史总星内容在。"""
    client, db = _make_client(tmp_path, monkeypatch, _seed_straddle)
    with client:
        conn = get_conn(db)
        conn.execute(
            "INSERT INTO board_cache (period, label, as_of, payload, computed_at)"
            " VALUES ('total', 'all', '2099-01-01T00:00:00Z', '[]', '2099-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()
        text = client.get("/?week=2026-W31").text  # 2026-W31 期末 2026-08-02：a/straddle 06-15 快照在 as_of 前
        assert "a/straddle" in text  # 实时降级总星内容（未读脏缓存）
        assert "暂无可展示数据" not in text


# ===== jobs 层：daily_job 后 board_cache 3 行；预计算失败不阻断 AI 段 =====


def _make_logger():
    logger = logging.getLogger("test-board-cache")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    records: list[logging.LogRecord] = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger.addHandler(ListHandler())
    return logger, records


class _FakeCtxClient:
    """假 async context manager（GitHubClient / DeepSeekClient 替身）：daily_job 只用到 with 生命周期。"""

    def __init__(self, token=""):
        self.token = token

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_daily_job_deps(monkeypatch, logger, ai_calls):
    async def fake_run_daily(client, conn, *, log=None):
        cur = conn.execute(
            "INSERT INTO repos (full_name, node_id, dead, source, created_at) VALUES (?, ?, 0, 'initial', ?)",
            ("a/live", "nid-live", "2026-08-08T00:00:00Z"),
        )
        conn.execute("INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, '2026-08-09T00:00:00Z', 100)", (cur.lastrowid,))
        conn.commit()
        return DailyStats()

    async def fake_ensure(conn, ai_client, *, now, log, github_client):
        ai_calls.append(1)

    monkeypatch.setattr("app.jobs.get_job_logger", lambda: logger)
    monkeypatch.setattr("app.jobs.GitHubClient", _FakeCtxClient)
    monkeypatch.setattr("app.jobs.run_daily", fake_run_daily)
    monkeypatch.setattr("app.jobs.DeepSeekClient", _FakeCtxClient)
    monkeypatch.setattr("app.jobs.ensure_daily_ai", fake_ensure)
    monkeypatch.setattr("app.jobs.utc_now_iso", lambda: AS_OF)


def test_daily_job_precomputes_three_rows(tmp_path, monkeypatch):
    """daily_job：run_daily 成功后、AI 段之前落三口径缓存；AI 段照常执行；预计算日志留痕。"""
    db = tmp_path / "radar.db"
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    logger, records = _make_logger()
    ai_calls: list[int] = []
    _patch_daily_job_deps(monkeypatch, logger, ai_calls)

    asyncio.run(daily_job())

    conn = get_conn(db)
    try:
        rows = conn.execute("SELECT period, label, as_of FROM board_cache ORDER BY period").fetchall()
        assert [(r["period"], r["label"]) for r in rows] == [
            ("quarter", "2026-Q3"),
            ("total", "all"),
            ("week", "2026-W32"),
        ]
        assert all(r["as_of"] == AS_OF for r in rows)
    finally:
        conn.close()
    assert ai_calls == [1]  # AI 段正常执行
    assert any("榜单预计算完成" in r.getMessage() for r in records)


def test_daily_job_precompute_failure_does_not_block_ai(tmp_path, monkeypatch):
    """预计算整段异常吞掉记 ERROR：不抛出、AI 段照常执行（页面缺缓存降级实时算是天然兜底）。"""
    db = tmp_path / "radar.db"
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    logger, records = _make_logger()
    ai_calls: list[int] = []
    _patch_daily_job_deps(monkeypatch, logger, ai_calls)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟预计算失败")

    monkeypatch.setattr("app.jobs.precompute_boards", boom)

    asyncio.run(daily_job())  # 不抛出
    assert ai_calls == [1]  # AI 段不受影响
    assert any("榜单预计算异常" in r.getMessage() for r in records)
