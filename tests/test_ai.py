"""T-011 AI 服务测试：DeepSeekClient（MockTransport 离线）＋ ensure_weekly_ai（fake client 注入）＋ daily_job 接线。

tmp_path 独立库＋RADAR_DB_PATH 覆盖（口径照 tests/test_tags.py）；全 mock，禁止真实网络调用；
异步用例统一 asyncio.run 驱动（不引 pytest-asyncio，与 test_jobs.py 同口径）。

造榜手法照 tests/test_report.py：周口径上榜 = 7 天窗口两端快照（5~9 天滑动窗口内）；
单快照仓库首周缺席不上榜（懒口径"未上榜不译不生成"的构造依据）。
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.ai import DeepSeekAuthError, DeepSeekClient, DeepSeekError, ensure_weekly_ai, has_cjk
from app.db import get_conn, init_db
from app.jobs import daily_job

AS_OF_DT = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)  # 周日 12:00，所在 ISO 周 = 2026-W32
WEEK1 = "2026-W32"  # 与 app/web/routes.py 的 _week_label 同口径（isocalendar），此处硬编码锁死
WEEK2 = "2026-W33"  # AS_OF_DT + 7 天


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_repo(
    conn,
    name,
    *,
    description_en,
    language="Python",
    topics=(),
    description_zh=None,
    listed=True,
    base=1000,
    delta=100,
):
    """插仓库＋周窗口两端快照（listed=False 只插端点一张 → 首周缺席不上榜）；返回 repo_id。"""
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, 'test', '2026-07-01T00:00:00Z')",
        (name, f"node-{name}", description_en, description_zh, language, json.dumps(list(topics))),
    )
    repo_id = cur.lastrowid
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
        (repo_id, _iso(AS_OF_DT), base + delta),
    )
    if listed:
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, _iso(AS_OF_DT - timedelta(days=7)), base),
        )
    conn.commit()
    return repo_id


class FakeDeepSeekClient:
    """假 DeepSeek client：translate 回显"译文-<原文>"，recommend 记录全部入参回显"推荐语-<full_name>"。

    fail_translate_for（按简介文本）/ fail_recommend_for（按 full_name）注入单条失败。
    """

    def __init__(self, *, fail_translate_for=(), fail_recommend_for=()):
        self.fail_translate_for = set(fail_translate_for)
        self.fail_recommend_for = set(fail_recommend_for)
        self.translate_calls: list[str] = []
        self.recommend_calls: list[dict] = []

    async def translate(self, text: str) -> str:
        self.translate_calls.append(text)
        if text in self.fail_translate_for:
            raise DeepSeekError("模拟翻译失败")
        return f"译文-{text}"

    async def recommend(self, *, full_name, description, language, delta, stars, categories) -> str:
        self.recommend_calls.append(
            {
                "full_name": full_name,
                "description": description,
                "language": language,
                "delta": delta,
                "stars": stars,
                "categories": list(categories),
            }
        )
        if full_name in self.fail_recommend_for:
            raise DeepSeekError("模拟推荐语失败")
        return f"推荐语-{full_name}"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")  # ensure 的 key 检查：默认有 key，空 key 用例自行覆盖
    db = tmp_path / "t.db"
    init_db(db)
    c = get_conn(db)
    yield c
    c.close()


def _run_ensure(conn, client, now=AS_OF_DT):
    return asyncio.run(ensure_weekly_ai(conn, client, now=now))


def _zh_map(conn):
    return {r["full_name"]: r["description_zh"] for r in conn.execute("SELECT full_name, description_zh FROM repos")}


def _recommend_names(conn):
    return {
        r["full_name"]
        for r in conn.execute("SELECT r.full_name FROM recommendations c JOIN repos r ON r.id = c.repo_id")
    }


# ---------- DeepSeekClient（MockTransport 离线，不真打 API） ----------


async def _no_sleep(_seconds: float) -> None:
    """测试用假 sleep：重试等待不真实流逝（与 test_github.py 同手法）。"""


def test_client_posts_chat_completions_payload():
    """请求契约：URL/model/Authorization 头/低 temperature/消息结构；返回 content 剥首尾空白。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "  译文本体  "}}]})

    client = DeepSeekClient("test-key", transport=httpx.MockTransport(handler))
    assert asyncio.run(client.translate("A fast web framework")) == "译文本体"
    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "deepseek-chat"
    assert seen["body"]["temperature"] <= 0.3  # 任务书口径：temperature 调低
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]
    assert seen["body"]["messages"][1]["content"] == "A fast web framework"
    asyncio.run(client.aclose())


def test_client_retries_5xx_once_then_succeeds():
    """5xx 重试一次后成功：共 2 次请求，返回第二次的 content。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = DeepSeekClient("k", transport=httpx.MockTransport(handler), sleep=_no_sleep)
    assert asyncio.run(client.translate("x")) == "ok"
    assert len(calls) == 2
    asyncio.run(client.aclose())


def test_client_4xx_fails_fast_without_retry():
    """4xx（非 401）不重试直接抛 DeepSeekError。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad request")

    client = DeepSeekClient("k", transport=httpx.MockTransport(handler), sleep=_no_sleep)
    with pytest.raises(DeepSeekError, match="HTTP 400"):
        asyncio.run(client.translate("x"))
    assert len(calls) == 1
    asyncio.run(client.aclose())


def test_client_401_raises_auth_error_without_retry():
    """401 → DeepSeekAuthError（key 修复指引），不重试；402/403 账户类错误同样直通（评审低-3）。"""
    for status, hint in ((401, "DEEPSEEK_API_KEY"), (402, "余额与权限"), (403, "余额与权限")):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(status, text="account error")

        client = DeepSeekClient("k", transport=httpx.MockTransport(handler), sleep=_no_sleep)
        with pytest.raises(DeepSeekAuthError, match=hint):
            asyncio.run(client.translate("x"))
        assert len(calls) == 1  # 确定性错误不重试
        asyncio.run(client.aclose())


def test_client_malformed_json_retried_once_then_raises():
    """200 但 JSON 畸形：重试一次后仍畸形 → 抛 DeepSeekError（共 2 次请求）。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text="not json")

    client = DeepSeekClient("k", transport=httpx.MockTransport(handler), sleep=_no_sleep)
    with pytest.raises(DeepSeekError, match="响应畸形"):
        asyncio.run(client.translate("x"))
    assert len(calls) == 2
    asyncio.run(client.aclose())


def test_client_timeout_retried_once_then_raises():
    """超时：重试一次后仍超时 → 抛 DeepSeekError（共 2 次请求）。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.TimeoutException("boom")

    client = DeepSeekClient("k", transport=httpx.MockTransport(handler), sleep=_no_sleep)
    with pytest.raises(DeepSeekError, match="重试 1 次后放弃"):
        asyncio.run(client.translate("x"))
    assert len(calls) == 2
    asyncio.run(client.aclose())


def test_client_empty_key_fails_before_any_request():
    """空 key 在使用点拦截：不发请求直接 DeepSeekAuthError（与 GitHubClient 同姿态）。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={})

    client = DeepSeekClient("", transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekAuthError, match="API key 为空"):
        asyncio.run(client.translate("x"))
    assert calls == []
    asyncio.run(client.aclose())


# ---------- ensure_weekly_ai：懒翻译＋推荐理由＋幂等＋降级 ----------


def test_translate_skips_cjk_null_and_already_translated(conn):
    """懒翻译跳过口径：原文含中文（含中英混排）/ description_en NULL / 已有 description_zh 一律不送译。"""
    _add_repo(conn, "a/cjk", description_en="已有中文描述的项目")
    _add_repo(conn, "a/mixed", description_en="web 框架 with 中文混排")
    _add_repo(conn, "a/no-desc", description_en=None)
    _add_repo(conn, "a/done", description_en="already translated", description_zh="已译")
    _add_repo(conn, "a/todo", description_en="needs translation")
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert fake.translate_calls == ["needs translation"]  # 只有 a/todo 送译
    zh = _zh_map(conn)
    assert zh["a/todo"] == "译文-needs translation"
    assert zh["a/cjk"] is None and zh["a/mixed"] is None and zh["a/no-desc"] is None
    assert zh["a/done"] == "已译"  # 已译不被覆盖
    assert stats["translated"] == 1 and stats["translate_failed"] == 0
    # 推荐语对上榜集全部生成（含无简介/中文原文的，不受翻译跳过影响）
    assert {c["full_name"] for c in fake.recommend_calls} == {"a/cjk", "a/mixed", "a/no-desc", "a/done", "a/todo"}
    no_desc = next(c for c in fake.recommend_calls if c["full_name"] == "a/no-desc")
    assert no_desc["description"] == "（无简介）"


def test_translate_backfills_listed_repos(conn):
    """上榜无翻译项目批量补译：UPDATE 按 full_name 落库正确、计数正确。"""
    _add_repo(conn, "a/one", description_en="first project")
    _add_repo(conn, "a/two", description_en="second project", language="Go", topics=["ai"])
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats["listed"] == 2
    assert stats["translated"] == 2 and stats["translate_failed"] == 0
    assert _zh_map(conn) == {"a/one": "译文-first project", "a/two": "译文-second project"}


def test_recommend_inserted_with_repo_id_week_and_text(conn):
    """推荐语落库三要素（repo_id＋ISO 周标签＋text）；入参携带跨榜累加的分类榜名。"""
    repo_id = _add_repo(conn, "a/py-ai", description_en="ai toolkit", topics=["ai"])  # Python 语言榜＋AI与智能 主题榜
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats["listed"] == 1 and stats["recommended"] == 1 and stats["recommend_failed"] == 0
    rows = conn.execute("SELECT repo_id, report_week, text FROM recommendations").fetchall()
    assert [(r["repo_id"], r["report_week"], r["text"]) for r in rows] == [(repo_id, WEEK1, "推荐语-a/py-ai")]
    call = fake.recommend_calls[0]
    assert call["full_name"] == "a/py-ai"
    assert call["description"] == "译文-ai toolkit"  # 优先用本轮刚译好的中文
    assert call["categories"] == ["Python", "AI与智能"]  # 语言榜 label＋主题榜 label 跨榜累加
    assert call["language"] == "Python" and call["delta"] == 100 and call["stars"] == 1100


def test_same_week_rerun_idempotent(conn):
    """同周跑两次幂等：第二轮翻译/推荐全跳过（已译＋同周已存在），零 API 调用、库内零变化。"""
    _add_repo(conn, "a/one", description_en="first project")
    fake = FakeDeepSeekClient()
    stats1 = _run_ensure(conn, fake)
    assert stats1["translated"] == 1 and stats1["recommended"] == 1
    calls_after_first = (len(fake.translate_calls), len(fake.recommend_calls))
    stats2 = _run_ensure(conn, fake)
    assert stats2["listed"] == 1  # 上榜集照算
    assert stats2["translated"] == 0 and stats2["recommended"] == 0
    assert (len(fake.translate_calls), len(fake.recommend_calls)) == calls_after_first
    assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 1


def test_new_week_regenerates_recommendation(conn):
    """跨周生成新行（每周重新生成）：新周标签 INSERT，历史周保留；已译不重译。"""
    repo_id = _add_repo(conn, "a/one", description_en="first project")
    fake = FakeDeepSeekClient()
    _run_ensure(conn, fake)
    # 模拟下周每日采集：第二周窗口（8/9→8/16）两端齐备，项目仍在榜上
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
        (repo_id, _iso(AS_OF_DT + timedelta(days=7)), 1500),
    )
    conn.commit()
    stats2 = _run_ensure(conn, fake, now=AS_OF_DT + timedelta(days=7))
    assert stats2["listed"] == 1 and stats2["recommended"] == 1
    assert stats2["translated"] == 0
    weeks = {
        r["report_week"]
        for r in conn.execute("SELECT report_week FROM recommendations WHERE repo_id = ?", (repo_id,))
    }
    assert weeks == {WEEK1, WEEK2}


def test_unlisted_repo_untouched(conn):
    """懒口径：池内未上榜仓库（单快照首周缺席）不译不生成推荐语。"""
    _add_repo(conn, "a/listed", description_en="on board")
    _add_repo(conn, "a/newbie", description_en="off board", listed=False)
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats["listed"] == 1
    assert fake.translate_calls == ["on board"]
    zh = _zh_map(conn)
    assert zh["a/listed"] == "译文-on board" and zh["a/newbie"] is None
    assert _recommend_names(conn) == {"a/listed"}


def test_single_item_failure_degrades(conn, caplog):
    """单条失败降级：ensure 不抛、记 WARNING、其余项目正常、失败计入统计；翻译失败不连坐推荐。"""
    _add_repo(conn, "a/bad-translate", description_en="boom translate")
    _add_repo(conn, "a/bad-recommend", description_en="fine desc")
    _add_repo(conn, "a/good", description_en="good desc")
    fake = FakeDeepSeekClient(fail_translate_for={"boom translate"}, fail_recommend_for={"a/bad-recommend"})
    with caplog.at_level(logging.WARNING, logger="app.ai"):
        stats = _run_ensure(conn, fake)  # 不抛出即通过
    assert stats["translate_failed"] == 1 and stats["translated"] == 2
    assert stats["recommend_failed"] == 1 and stats["recommended"] == 2
    zh = _zh_map(conn)
    assert zh["a/bad-translate"] is None  # 翻译失败不留半成品
    assert zh["a/bad-recommend"] == "译文-fine desc" and zh["a/good"] == "译文-good desc"
    # 两段独立：a/bad-translate 翻译失败仍用英文原文生成推荐语；a/bad-recommend 无推荐语
    assert _recommend_names(conn) == {"a/bad-translate", "a/good"}
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AI 翻译失败，跳过 a/bad-translate" in m for m in messages)
    assert any("AI 推荐语生成失败，跳过 a/bad-recommend" in m for m in messages)


def test_empty_key_returns_zero_stats(conn, monkeypatch):
    """降级：DEEPSEEK_API_KEY 为空 → 返回零统计、零 API 调用、库内零变化、不抛。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    _add_repo(conn, "a/one", description_en="first project")
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats == {"listed": 0, "translated": 0, "translate_failed": 0, "recommended": 0, "recommend_failed": 0}
    assert fake.translate_calls == [] and fake.recommend_calls == []
    assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 0
    assert _zh_map(conn)["a/one"] is None


def test_auth_error_passes_through(conn):
    """auth 直通契约（评审低-1）：DeepSeekAuthError 不被单条 WARNING 吞掉、当条不落库、推荐段不执行。"""
    _add_repo(conn, "a/one", description_en="first project")

    class AuthFailClient(FakeDeepSeekClient):
        async def translate(self, text: str) -> str:
            raise DeepSeekAuthError("模拟 key 无效")

    with pytest.raises(DeepSeekAuthError):
        _run_ensure(conn, AuthFailClient())
    assert _zh_map(conn)["a/one"] is None
    assert _recommend_names(conn) == set()  # 翻译段直通后推荐段不执行，库内零写入


def test_partial_failure_converges_next_run_recommend(conn):
    """断点续跑收敛（评审低-2）：第一轮推荐失败翻译成功 → 第二轮零翻译调用（已译跳过）、推荐补齐。"""
    _add_repo(conn, "a/one", description_en="first project")
    stats1 = _run_ensure(conn, FakeDeepSeekClient(fail_recommend_for={"a/one"}))
    assert stats1["translated"] == 1 and stats1["recommend_failed"] == 1
    assert _recommend_names(conn) == set()
    fake2 = FakeDeepSeekClient()
    stats2 = _run_ensure(conn, fake2)
    assert fake2.translate_calls == []  # 已译跳过：零翻译调用
    assert stats2["recommended"] == 1
    assert _recommend_names(conn) == {"a/one"}


def test_partial_failure_converges_next_run_translate(conn):
    """断点续跑收敛（评审低-2）：第一轮翻译失败推荐成功 → 第二轮只重译该条、同周已存在推荐不重生成。"""
    _add_repo(conn, "a/one", description_en="first project")
    stats1 = _run_ensure(conn, FakeDeepSeekClient(fail_translate_for={"first project"}))
    assert stats1["translate_failed"] == 1 and stats1["recommended"] == 1  # 翻译失败不连坐推荐
    fake2 = FakeDeepSeekClient()
    stats2 = _run_ensure(conn, fake2)
    assert fake2.translate_calls == ["first project"]  # 只重译该条
    assert stats2["recommended"] == 0 and fake2.recommend_calls == []  # 同周已存在：推荐零调用
    assert _zh_map(conn)["a/one"] == "译文-first project"


def test_has_cjk_detection():
    """CJK 判定：纯英文/空串 False，含任一汉字（含中英混排）True。"""
    assert has_cjk("pure english text") is False
    assert has_cjk("") is False
    assert has_cjk("已有中文") is True
    assert has_cjk("web 框架") is True


# ---------- daily_job 接线：AI 失败永不阻断快照主流程 ----------


class _FakeGitHubClient:
    """假 GitHub client：daily_job 只要求异步上下文管理器形态（run_daily 已另行替换，不发请求）。"""

    def __init__(self, token: str) -> None:
        self.token = token

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


def test_daily_job_survives_ai_failure(tmp_path, monkeypatch, caplog):
    """ensure_weekly_ai 整轮抛错：daily_job 仍正常完成、记 ERROR 不抛出；AI 在 run_daily 之后串行。"""
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []

    async def fake_run_daily(client, conn, *, log=None):
        calls.append("run_daily")

    async def fake_ensure(conn, client, *, now, log=None):
        calls.append("ensure")
        raise RuntimeError("模拟 AI 整轮失败")

    monkeypatch.setattr("app.jobs.GitHubClient", _FakeGitHubClient)
    monkeypatch.setattr("app.jobs.run_daily", fake_run_daily)
    monkeypatch.setattr("app.jobs.ensure_weekly_ai", fake_ensure)
    # get_job_logger 落文件且 propagate=False：换成可传播的测试 logger 供 caplog 断言
    monkeypatch.setattr("app.jobs.get_job_logger", lambda: logging.getLogger("test-daily-job"))

    with caplog.at_level(logging.ERROR, logger="test-daily-job"):
        asyncio.run(daily_job())  # 不抛出即通过
    assert calls == ["run_daily", "ensure"]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("AI 周度生成整轮异常" in m for m in errors)


def test_daily_job_with_empty_key_behaves_as_before(tmp_path, monkeypatch):
    """key 缺失场景 daily_job 行为与接线前一致：run_daily 正常跑，真 ensure 内部降级零写入不抛。"""
    db = tmp_path / "jobs.db"
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    calls = []

    async def fake_run_daily(client, conn, *, log=None):
        calls.append("run_daily")

    monkeypatch.setattr("app.jobs.GitHubClient", _FakeGitHubClient)
    monkeypatch.setattr("app.jobs.run_daily", fake_run_daily)

    asyncio.run(daily_job())  # 真 ensure_weekly_ai：key 空 → 记 INFO 返回零统计
    assert calls == ["run_daily"]
    conn = get_conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 0
    finally:
        conn.close()
