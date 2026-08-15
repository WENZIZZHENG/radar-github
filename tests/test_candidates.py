"""T-032 候选词扫描与只读页测试：词频统计 SQL / 阈值+归一未命中判定 / AI 批量解析（含"不建议收录"与 JSON 容错）/
扫描落库覆盖与降级（AI 失败、key 缺失不清旧表）/ 页面渲染与两种空态 / 周一触发判定与 daily_job 链路接入。

打法照 tests/test_jobs.py（FakeClient + tmp_path 真实 sqlite）与 tests/test_ai.py（MockTransport 离线）；
异步用例统一 asyncio.run 驱动（不引 pytest-asyncio）；页面用例照 tests/test_web.py（TestClient 打真实 app）。
"""

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import app.jobs as jobs_module  # 同步后台任务/状态的模块级引用（运行时取最新值，非 import 时快照）
from app.ai import DeepSeekClient, DeepSeekError
from app.candidates import NOT_RECOMMENDED, is_known_term, scan_candidates, topic_frequencies
from app.classify import load_topics
from app.db import get_conn, init_db
from app.jobs import _candidate_scan_due, daily_job
from app.main import app

SCAN_DT = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)  # 2026-08-17 周一（UTC）
TOPICS_PATH = "config/topics.yaml"  # 词表（相对项目根）


def _seed_repo(conn, full_name, *, topics=(), dead=0) -> int:
    """插一个带 topics 的仓库（候选扫描只关心 topics/dead 字段）。"""
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at)"
        " VALUES (?, ?, NULL, NULL, 'Python', ?, ?, 'test', '2026-07-01T00:00:00Z')",
        (full_name, f"node-{full_name}", json.dumps(list(topics)), dead),
    )
    conn.commit()
    return cur.lastrowid


class FakeSuggestClient:
    """假 DeepSeek client：suggest_topics 按 mapping 返回（缺省"不建议收录"）；fail=True 抛 DeepSeekError。"""

    def __init__(self, mapping=None, fail=False):
        self.mapping = mapping or {}
        self.fail = fail
        self.calls: list[tuple[list, list]] = []

    async def suggest_topics(self, terms, topic_names):
        self.calls.append((list(terms), list(topic_names)))
        if self.fail:
            raise DeepSeekError("模拟 AI 调用失败")
        return {t: self.mapping.get(t, NOT_RECOMMENDED) for t in terms}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")  # 默认有 key，key 缺失用例自行覆盖
    db = tmp_path / "c.db"
    init_db(db)
    c = get_conn(db)
    yield c
    c.close()


def _run_scan(conn, client, now=SCAN_DT):
    return asyncio.run(scan_candidates(conn, client, now=now))


# ---------- 词频统计 SQL ----------


def test_topic_frequencies_counts_pool_topics(conn):
    """词频统计 SQL：json_each 展开 repos.topics 按词计数（dead=0 仓）；dead 仓不计。"""
    _seed_repo(conn, "a/one", topics=["ai", "llm", "rag"])
    _seed_repo(conn, "a/two", topics=["ai", "python"])
    _seed_repo(conn, "a/dead", topics=["ai", "claude"], dead=1)
    freq = topic_frequencies(conn)
    assert freq["ai"] == 2
    assert freq["llm"] == 1 and freq["python"] == 1 and freq["rag"] == 1
    assert "claude" not in freq  # dead 仓不计


# ---------- 未命中判定（单复数归一）与阈值过滤 ----------


def test_plural_normalized_and_low_freq_filtered(conn):
    """未命中判定（决策 3 v2 归一）与阈值过滤：
    - 纯函数：词表单数（frontend）覆盖池内复数（frontends）不报未命中；不做通用去 s（cs 不命中，防 css 误伤）；
    - 扫描路径：词频低于阈值（< 5）的词不送 AI、不落库。"""
    table = load_topics(__import__("pathlib").Path(TOPICS_PATH))
    assert is_known_term("ai", table) is True  # 精确命中
    assert is_known_term("frontends", table) is True  # 词表 frontend（单数）+ s → 归一命中（agents 同理不误报）
    assert is_known_term("not-a-real-topic-xyz", table) is False
    assert is_known_term("cs", table) is False  # 不做通用去 s：css 的词条是 css，cs 不命中（防误伤）

    for i in range(6):
        _seed_repo(conn, f"a/r{i}", topics=["frontends"])  # 复数：归一命中词表，词频 6 也不算候选
    for i in range(6, 10):
        _seed_repo(conn, f"a/b{i}", topics=["super-rare-new-thing"])  # 词频 4 < 阈值 5
    client = FakeSuggestClient()
    stats = _run_scan(conn, client)
    assert stats["scanned"] is True and stats["candidates"] == 0
    assert client.calls == []  # 无候选不调 AI
    assert conn.execute("SELECT COUNT(*) FROM topic_candidates").fetchone()[0] == 0
    assert conn.execute("SELECT term_count FROM candidate_scans WHERE id = 1").fetchone()[0] == 0


# ---------- AI 批量解析（含"不建议收录"与 JSON 容错） ----------


def test_suggest_topics_parses_batch_json_and_tolerates_malformed():
    """AI 输出解析（§15.2）：批量一次调用（请求体含全部候选词与主题名，不含仓库数据）；
    JSON 对象逐词解析（含"不建议收录"值）；markdown 围栏容错；畸形/无 JSON → DeepSeekError（调用方整段降级）。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        content = '```json\n{"claude-code": "AI与智能", "hacktoberfest": "不建议收录"}\n```'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = DeepSeekClient("test-key", transport=httpx.MockTransport(handler))
    out = asyncio.run(client.suggest_topics(["claude-code", "hacktoberfest"], ["AI与智能", "前端/UI"]))
    assert out == {"claude-code": "AI与智能", "hacktoberfest": "不建议收录"}
    user = seen["body"]["messages"][1]["content"]
    assert "- claude-code" in user and "AI与智能" in user  # 输入只含候选词与主题名
    assert "仓库" not in user and "repo" not in user.lower()  # §15.4 红线：不含仓库数据
    assert seen["body"]["temperature"] <= 0.3  # 低温度：归类求稳
    asyncio.run(client.aclose())

    for bad in ("不是 JSON", '{"a": "b"', "[]"):
        def handler_bad(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": bad}}]})

        client2 = DeepSeekClient("test-key", transport=httpx.MockTransport(handler_bad))
        with pytest.raises(DeepSeekError, match="解析失败"):
            asyncio.run(client2.suggest_topics(["a"], ["主题"]))
        asyncio.run(client2.aclose())


# ---------- 扫描主路径：成功落库覆盖 / 无候选清旧 / 降级不清旧 ----------


def test_scan_success_writes_and_replaces(conn):
    """主路径（§15.2）：达标未命中词 → AI 批量建议 → 全量覆盖落库（旧行消失，只留最近一轮）；
    "不建议收录"词照常落库；candidate_scans 记录扫描时刻与候选数。"""
    conn.execute(
        "INSERT INTO topic_candidates (term, suggested_topic, pool_count, scanned_at)"
        " VALUES ('old-term', 'AI与智能', 99, '2026-08-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO candidate_scans (id, scanned_at, term_count) VALUES (1, '2026-08-01T00:00:00Z', 1)")
    conn.commit()
    for i in range(6):
        # brand-new-x 保证不在封板词表（2026-08-15 收词后 claude-code 已入词表，不能再当"未命中"种子）
        _seed_repo(conn, f"a/r{i}", topics=["brand-new-x", "hacktoberfest", "ai"])  # ai 命中词表不算候选
    client = FakeSuggestClient(mapping={"brand-new-x": "AI与智能", "hacktoberfest": "不建议收录"})
    stats = _run_scan(conn, client)
    assert stats["scanned"] is True and stats["candidates"] == 2
    assert stats["kept"] == 2 and stats["rejected"] == 1 and stats["dropped"] == 0
    rows = conn.execute(
        "SELECT term, suggested_topic, pool_count, scanned_at FROM topic_candidates ORDER BY pool_count DESC, term"
    ).fetchall()
    assert [(r["term"], r["suggested_topic"], r["pool_count"]) for r in rows] == [
        ("brand-new-x", "AI与智能", 6),
        ("hacktoberfest", "不建议收录", 6),
    ]
    assert all(r["scanned_at"] == "2026-08-17T12:00:00Z" for r in rows)  # 全量覆盖：旧行消失
    scan = conn.execute("SELECT scanned_at, term_count FROM candidate_scans WHERE id = 1").fetchone()
    assert scan["scanned_at"] == "2026-08-17T12:00:00Z" and scan["term_count"] == 2
    assert client.calls[0][0] == ["brand-new-x", "hacktoberfest"]  # 批量一次调用：候选词全量入参
    assert "AI与智能" in client.calls[0][1]  # 主题名来自词表 label


def test_scan_no_candidates_clears_previous_round(conn):
    """§15.1 自动消失：本周无候选（全部命中词表/降频）→ 清空上一轮候选（只留最近一轮）+ 扫描记录 term_count=0。"""
    conn.execute(
        "INSERT INTO topic_candidates (term, suggested_topic, pool_count, scanned_at)"
        " VALUES ('old-term', 'AI与智能', 9, '2026-08-01T00:00:00Z')"
    )
    conn.execute("INSERT INTO candidate_scans (id, scanned_at, term_count) VALUES (1, '2026-08-01T00:00:00Z', 1)")
    conn.commit()
    for i in range(6):
        _seed_repo(conn, f"a/r{i}", topics=["ai"])  # ai 命中词表：不算候选
    client = FakeSuggestClient()
    stats = _run_scan(conn, client)
    assert stats["scanned"] is True and stats["candidates"] == 0
    assert conn.execute("SELECT COUNT(*) FROM topic_candidates").fetchone()[0] == 0  # 旧候选清空
    assert conn.execute("SELECT term_count FROM candidate_scans WHERE id = 1").fetchone()[0] == 0
    assert client.calls == []


def test_scan_ai_failure_and_empty_key_keep_previous_round(conn, monkeypatch):
    """降级（§15.3）：AI 调用失败 / key 未配置 → 整段跳过不写表不清表（页面显示上一轮）、不产生扫描记录；
    key 缺失走 INFO 降级不调 AI（与 ensure_daily_ai 同口径）。"""
    conn.execute(
        "INSERT INTO topic_candidates (term, suggested_topic, pool_count, scanned_at)"
        " VALUES ('old-term', 'AI与智能', 9, '2026-08-01T00:00:00Z')"
    )
    conn.commit()
    for i in range(6):
        _seed_repo(conn, f"a/r{i}", topics=["brand-new-x"])  # 词表外候选词（claude-code 已入词表，见上注）
    # AI 调用失败
    stats = _run_scan(conn, FakeSuggestClient(fail=True))
    assert stats["scanned"] is False
    assert conn.execute("SELECT COUNT(*) FROM topic_candidates").fetchone()[0] == 1  # 旧行保留
    assert conn.execute("SELECT COUNT(*) FROM candidate_scans").fetchone()[0] == 0  # 无扫描记录
    # key 未配置
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    client = FakeSuggestClient()
    stats2 = _run_scan(conn, client)
    assert stats2["scanned"] is False
    assert client.calls == []  # 不调 AI
    assert conn.execute("SELECT COUNT(*) FROM topic_candidates").fetchone()[0] == 1  # 仍保留


# ---------- P7 只读页：列表渲染 / 两种空态 / 首页入口 ----------


def _make_web_client(tmp_path, monkeypatch):
    """页面测试环境：RADAR_DB_PATH 指向 tmp 独立库（TestClient 请求级生效，同 test_web.py），调度关闭。"""
    db = tmp_path / "web.db"
    init_db(db)
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    return db, TestClient(app)


def test_candidates_page_rows_link_and_two_empty_states(tmp_path, monkeypatch):
    """P7 页面（§15.1/§15.3）：有候选 → 列表渲染（次数降序、"不建议收录"灰显、扫描日期切片）；
    首页元信息行有"候选词"入口（不占顶栏）；两种空态——扫描过但无候选（主文案）/
    从未成功扫描（注明"扫描未运行过（AI 未配置）"）。"""
    db, client = _make_web_client(tmp_path, monkeypatch)
    conn = get_conn(db)
    conn.execute(
        "INSERT INTO topic_candidates (term, suggested_topic, pool_count, scanned_at) VALUES"
        " ('claude-code', 'AI与智能', 81, '2026-08-10T00:00:00Z'),"
        " ('hacktoberfest', '不建议收录', 98, '2026-08-10T00:00:00Z')"
    )
    conn.execute("INSERT INTO candidate_scans (id, scanned_at, term_count) VALUES (1, '2026-08-10T00:00:00Z', 2)")
    conn.commit()
    conn.close()

    resp = client.get("/topic-candidates")
    assert resp.status_code == 200
    html = resp.text
    assert "claude-code" in html and "AI与智能" in html and "81 次" in html and "2026-08-10" in html
    assert html.index("hacktoberfest") < html.index("claude-code")  # 按次数降序：98 在前

    home = client.get("/").text
    assert 'href="/topic-candidates"' in home and "候选词" in home  # P1 元信息行入口（§15.1 小字链接）
    tabs = home[home.index('<nav class="tabs"') : home.index("</nav>")]
    assert tabs.count("<a ") == 5  # 顶栏导航原样 5 项（§15.1：候选词不占顶栏，5 项上限不破）

    # 空态 A：扫描过但无候选（有 scan 记录，无候选行）
    conn = get_conn(db)
    conn.execute("DELETE FROM topic_candidates")
    conn.commit()
    conn.close()
    empty1 = client.get("/topic-candidates").text
    assert "当前无候选词——上周扫描未发现高频未命中词" in empty1
    assert "扫描未运行过" not in empty1

    # 空态 B：从未成功扫描（无 scan 记录）→ 注明"扫描未运行过（AI 未配置）"
    conn = get_conn(db)
    conn.execute("DELETE FROM candidate_scans")
    conn.commit()
    conn.close()
    empty2 = client.get("/topic-candidates").text
    assert "当前无候选词——上周扫描未发现高频未命中词" in empty2
    assert "扫描未运行过（AI 未配置）" in empty2
    assert 'href="/"' in empty2  # P7 页内返回首页链接


# ---------- 触发判定与 daily_job 链路接入 ----------


class _FakeGitHub:
    """链路测试最小假 GitHub client：空池（无 nodes、search 空页），run_daily 跑通即可。"""

    async def fetch_repos_by_ids(self, ids):
        return [None] * len(ids)

    async def search_repositories(self, query, *, sort="stars", per_page=100, page=1):
        return {"total_count": 0, "items": []}


class _FakeGitHubCM:
    """模拟 GitHubClient 的 async with 协议（同 test_jobs.py 手法）。"""

    async def __aenter__(self):
        return _FakeGitHub()

    async def __aexit__(self, *exc_info):
        pass


def test_scan_due_only_on_monday_and_daily_job_wiring(tmp_path, monkeypatch):
    """触发（§15.2）：_candidate_scan_due 仅 UTC 周一（weekday()==0，与 _weekly_refill 同日判定）；
    daily_job 全链路仅在周一调用 scan_candidates（spy），非周一零调用（周一以外零行为变化）。"""
    MONDAY = datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc)
    SUNDAY = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
    SATURDAY = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert _candidate_scan_due(MONDAY) is True
    assert _candidate_scan_due(SUNDAY) is False
    assert _candidate_scan_due(SATURDAY) is False

    calls = []

    async def fake_scan(*a, **k):
        calls.append(1)
        return {"scanned": True}

    class FakeMonDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return MONDAY

    class FakeSunDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return SUNDAY

    db = tmp_path / "radar.db"
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")  # AI 段走降级（key 未配置不算失败）
    init_db(db)
    monkeypatch.setattr("app.jobs.GitHubClient", lambda *a, **k: _FakeGitHubCM())
    monkeypatch.setattr("app.jobs.scan_candidates", fake_scan)
    with jobs_module._sync_state_lock:
        jobs_module._sync_state.update(
            running=False, started_at=None, last_finished_at=None, last_stats=None, last_error=None
        )
    # 周一：扫描段被调用
    monkeypatch.setattr("app.jobs.datetime", FakeMonDatetime)
    asyncio.run(daily_job())
    assert len(calls) == 1
    # 非周一（周日）：同一链路不调用扫描段（零行为变化）
    monkeypatch.setattr("app.jobs.datetime", FakeSunDatetime)
    asyncio.run(daily_job())
    assert len(calls) == 1  # 没有新增调用
