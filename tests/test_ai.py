"""T-011/T-017 AI 服务测试：DeepSeekClient（MockTransport 离线）＋ ensure_daily_ai（fake client 注入）＋ daily_job 接线。

tmp_path 独立库＋RADAR_DB_PATH 覆盖（口径照 tests/test_tags.py）；全 mock，禁止真实网络调用；
异步用例统一 asyncio.run 驱动（不引 pytest-asyncio，与 test_jobs.py 同口径）。

造榜手法照 tests/test_report.py：周口径上榜 = 7 天窗口两端快照（5~9 天滑动窗口内）；
单快照仓库首周缺席不上榜（懒口径"未上榜不译不生成"的构造依据）；季度上榜需 86~94 天窗口快照。
T-017 口径：翻译收窄为范围集 S = 三口径榜去重 ∪ 关注集；推荐语按维度分条（周/季/总星）。
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.ai import (
    DeepSeekAuthError,
    DeepSeekClient,
    DeepSeekError,
    ensure_daily_ai,
    has_cjk,
    recommend_missing,
)
from app.collector.github import GitHubAuthError
from app.db import get_conn, init_db
from app.jobs import daily_job

AS_OF_DT = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)  # 周日 12:00，所在 ISO 周 = 2026-W32
WEEK1 = "2026-W32"  # 与 app/web/routes.py 的 _week_label 同口径（isocalendar），此处硬编码锁死
WEEK2 = "2026-W33"  # AS_OF_DT + 7 天
QUARTER1 = "2026-Q3"  # AS_OF_DT 所在季度（8 月 → Q3）


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
    quarter=False,
    github_created_at=None,
):
    """插仓库＋周窗口两端快照（listed=False 只插端点一张 → 首周缺席不上榜）；quarter=True 再加 90 天前
    一张（86~94 天滑动窗口 → 季榜出席）；github_created_at（T-033）缺省 None（未回填）；返回 repo_id。"""
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at, github_created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, 'test', '2026-07-01T00:00:00Z', ?)",
        (name, f"node-{name}", description_en, description_zh, language, json.dumps(list(topics)), github_created_at),
    )
    repo_id = cur.lastrowid
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
        (repo_id, _iso(AS_OF_DT), base + delta),
    )
    if quarter:
        # 87 天前一张：第一周跨度 87 天（86~94 内出席），次周跨度 94 天（仍在窗口上限内，季榜继续出席）
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, _iso(AS_OF_DT - timedelta(days=87)), base),
        )
    if listed:
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, _iso(AS_OF_DT - timedelta(days=7)), base),
        )
    conn.commit()
    return repo_id


class FakeDeepSeekClient:
    """假 DeepSeek client：translate 回显"译文-<原文>"，recommend 记录全部入参回显"推荐语-<full_name>-<dimension>"，
    summarize 记录全部入参回显"概要-<full_name>"（T-024 新增）。

    fail_translate_for（按简介文本）/ fail_recommend_for（按 full_name）/ fail_summarize_for（按 full_name）
    注入单条失败（失败不区分维度）。
    """

    def __init__(self, *, fail_translate_for=(), fail_recommend_for=(), fail_summarize_for=()):
        self.fail_translate_for = set(fail_translate_for)
        self.fail_recommend_for = set(fail_recommend_for)
        self.fail_summarize_for = set(fail_summarize_for)
        self.translate_calls: list[str] = []
        self.recommend_calls: list[dict] = []
        self.summarize_calls: list[dict] = []

    async def translate(self, text: str) -> str:
        self.translate_calls.append(text)
        if text in self.fail_translate_for:
            raise DeepSeekError("模拟翻译失败")
        return f"译文-{text}"

    async def recommend(
        self, *, full_name, description, language, categories, dimension, delta=None, stars=None, readme=None, pool_days=None
    ) -> str:
        self.recommend_calls.append(
            {
                "full_name": full_name,
                "description": description,
                "language": language,
                "delta": delta,
                "stars": stars,
                "categories": list(categories),
                "dimension": dimension,
                "readme": readme,
                "pool_days": pool_days,  # T-018：新区仓入池语境（主榜仓 None）
            }
        )
        if full_name in self.fail_recommend_for:
            raise DeepSeekError("模拟推荐语失败")
        return f"推荐语-{full_name}-{dimension}"

    async def summarize(self, *, full_name, description, language, readme=None) -> str:
        self.summarize_calls.append(
            {"full_name": full_name, "description": description, "language": language, "readme": readme}
        )
        if full_name in self.fail_summarize_for:
            raise DeepSeekError("模拟概要生成失败")
        return f"概要-{full_name}"


class FakeGitHubClient:
    """假 GitHub client：fetch_readme 按 full_name 脚本返回 (text, sha)；可注入失败与 auth 错误。"""

    def __init__(self, *, readmes=None, fail_for=(), auth_for=()):
        self.readmes = readmes or {}
        self.fail_for = set(fail_for)
        self.auth_for = set(auth_for)
        self.readme_calls: list[str] = []

    async def fetch_readme(self, full_name: str) -> tuple[str | None, str | None]:
        self.readme_calls.append(full_name)
        if full_name in self.auth_for:
            raise GitHubAuthError("GitHub token 无效或已过期")
        if full_name in self.fail_for:
            raise RuntimeError("模拟 README 拉取失败")
        return self.readmes.get(full_name, (None, None))


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")  # ensure 的 key 检查：默认有 key，空 key 用例自行覆盖
    db = tmp_path / "t.db"
    init_db(db)
    c = get_conn(db)
    yield c
    c.close()


def _run_ensure(conn, client, now=AS_OF_DT, github_client=None):
    return asyncio.run(ensure_daily_ai(conn, client, now=now, github_client=github_client))


def _zh_map(conn):
    return {r["full_name"]: r["description_zh"] for r in conn.execute("SELECT full_name, description_zh FROM repos")}


def _recommend_names(conn):
    return {
        r["full_name"]
        for r in conn.execute("SELECT r.full_name FROM recommendations c JOIN repos r ON r.id = c.repo_id")
    }


def _recommend_rows(conn):
    """(full_name, dimension, period_label, text) 列表（断言落库形态用）。"""
    return [
        (r["full_name"], r["dimension"], r["period_label"], r["text"])
        for r in conn.execute(
            "SELECT r.full_name, c.dimension, c.period_label, c.text FROM recommendations c"
            " JOIN repos r ON r.id = c.repo_id ORDER BY c.dimension, c.period_label, r.full_name"
        )
    ]


def _calls_by_dim(fake, dimension):
    return [c for c in fake.recommend_calls if c["dimension"] == dimension]


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


def test_client_recommend_prompt_dimension_divergence():
    """分维度 prompt 分化钉死（2026-08-12 本人复验反馈）：周/季 system 必须要求明确写出当期增星数字，
    total system 禁止引用任何数字——两套文本肉眼可辨；delta 缺席时周/季退回软要求（不写死数字）。"""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "推荐语本体"}}]})

    client = DeepSeekClient("test-key", transport=httpx.MockTransport(handler))
    base = {"full_name": "a/one", "description": "desc", "language": "Python", "categories": ["Python"]}
    asyncio.run(client.recommend(**base, dimension="week", delta=150, stars=1100))
    asyncio.run(client.recommend(**base, dimension="quarter", delta=200, stars=1100))
    asyncio.run(client.recommend(**base, dimension="total"))
    asyncio.run(client.recommend(**base, dimension="week", stars=1100))  # delta 缺席（历史期次无增量行）
    asyncio.run(client.aclose())

    week_sys, week_user = bodies[0]["messages"][0]["content"], bodies[0]["messages"][1]["content"]
    assert "必须明确写出本周新增星数（150 星）" in week_sys
    assert "本周新增星数：150" in week_user
    quarter_sys = bodies[1]["messages"][0]["content"]
    assert "必须明确写出本季新增星数（200 星）" in quarter_sys
    total_sys, total_user = bodies[2]["messages"][0]["content"], bodies[2]["messages"][1]["content"]
    assert "不要引用任何具体数字" in total_sys
    assert "新增星数" not in total_user and "总星数" not in total_user  # total 输入不带数字
    soft_sys = bodies[3]["messages"][0]["content"]
    assert "必须明确写出" not in soft_sys and "结合本周增星" in soft_sys  # delta 缺席退回软要求


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
    """401 → DeepSeekAuthError（key 修复指引），不重试；402 及普通 403 账户类错误同样直通（评审低-3）。"""
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


def test_client_403_content_policy_raises_deep_seek_error_not_auth():
    """403 且响应含 content_policy_violation → DeepSeekError（单仓跳过），不得抛 DeepSeekAuthError（生产事故修复）。"""
    bodies = {
        "translate": ("x", {}),
        "recommend": (
            None,
            {
                "full_name": "a/b",
                "description": "desc",
                "language": "Python",
                "categories": ["Python"],
                "dimension": "week",
            },
        ),
        "summarize": (
            None,
            {
                "full_name": "a/b",
                "description": "desc",
                "language": "Python",
            },
        ),
    }

    def make_handler(calls: list):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(
                403,
                json={
                    "error": {
                        "message": "内容触发安全政策，类型：content_policy_violation",
                        "type": "content_policy_violation",
                    }
                },
            )

        return handler

    for method, (first_arg, kwargs) in bodies.items():
        calls: list = []
        client = DeepSeekClient("k", transport=httpx.MockTransport(make_handler(calls)), sleep=_no_sleep)
        coro = (
            getattr(client, method)(first_arg, **kwargs)
            if first_arg is not None
            else getattr(client, method)(**kwargs)
        )
        with pytest.raises(DeepSeekError, match="内容审核拦截") as exc_info:
            asyncio.run(coro)
        assert not isinstance(exc_info.value, DeepSeekAuthError)
        assert len(calls) == 1  # 4xx 不重试
        asyncio.run(client.aclose())


def test_client_403_plain_still_auth_error():
    """普通 403（无 content_policy 标记）仍维持 DeepSeekAuthError 直通。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, text="Forbidden")

    client = DeepSeekClient("k", transport=httpx.MockTransport(handler), sleep=_no_sleep)
    with pytest.raises(DeepSeekAuthError, match="余额与权限"):
        asyncio.run(client.translate("x"))
    assert len(calls) == 1
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
    """范围内翻译跳过口径：原文含中文（含中英混排）/ description_en NULL / 已有 description_zh 一律不送译。"""
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
    # 推荐语对上榜集全部生成（含无简介/中文原文的，不受翻译跳过影响）；每仓 3 维度（周＋季新区＋总星）
    # ＋ 1 条概要（T-024：summary 段只走 summarize 方法，不产生 recommend_calls）
    assert {c["full_name"] for c in fake.recommend_calls} == {"a/cjk", "a/mixed", "a/no-desc", "a/done", "a/todo"}
    assert {c["dimension"] for c in fake.recommend_calls} == {"week", "quarter", "total"}  # T-018：无 90 天快照 → 季维度走新区
    assert {c["full_name"] for c in fake.summarize_calls} == {"a/cjk", "a/mixed", "a/no-desc", "a/done", "a/todo"}
    no_desc_week = next(c for c in _calls_by_dim(fake, "week") if c["full_name"] == "a/no-desc")
    assert no_desc_week["description"] == "（无简介）"


def test_translate_backfills_listed_repos(conn):
    """上榜无翻译项目批量补译：UPDATE 按 full_name 落库正确、计数正确。"""
    _add_repo(conn, "a/one", description_en="first project")
    _add_repo(conn, "a/two", description_en="second project", language="Go", topics=["ai"])
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats["listed"] == 2
    assert stats["translated"] == 2 and stats["translate_failed"] == 0
    assert stats["recommended"] == 6  # T-018：2 仓 ×（周＋季新区＋总星）3 维度
    assert stats["summarized"] == 2  # T-024：2 仓各 1 条概要
    assert _zh_map(conn) == {"a/one": "译文-first project", "a/two": "译文-second project"}


def test_recommend_inserted_with_repo_id_week_and_text(conn):
    """推荐语落库三要素（repo_id＋维度＋期次标签＋text）；周维度入参携带跨榜累加的分类榜名与增量。
    T-024：概要行同库并存（'summary'/'all'，全页面同一条）。"""
    _add_repo(conn, "a/py-ai", description_en="ai toolkit", topics=["ai"])  # Python 语言榜＋AI与智能 主题榜
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats["listed"] == 1 and stats["recommended"] == 3 and stats["recommend_failed"] == 0
    rows = _recommend_rows(conn)
    assert rows == [
        ("a/py-ai", "quarter", QUARTER1, "推荐语-a/py-ai-quarter"),  # T-018：无 90 天快照 → 季维度新区
        ("a/py-ai", "summary", "all", "概要-a/py-ai"),  # T-024：概要行，period_label 恒 'all'
        ("a/py-ai", "total", "all", "推荐语-a/py-ai-total"),
        ("a/py-ai", "week", WEEK1, "推荐语-a/py-ai-week"),
    ]
    week_call = _calls_by_dim(fake, "week")[0]
    assert week_call["full_name"] == "a/py-ai"
    assert week_call["description"] == "译文-ai toolkit"  # 优先用本轮刚译好的中文
    assert week_call["categories"] == ["Python", "AI与智能"]  # 语言榜 label＋主题榜 label 跨榜累加
    assert week_call["language"] == "Python" and week_call["delta"] == 100 and week_call["stars"] == 1100
    assert week_call["pool_days"] is None  # 主榜仓不带入池语境
    q_call = _calls_by_dim(fake, "quarter")[0]
    assert q_call["pool_days"] == 7.0 and q_call["delta"] == 100  # 季新区：在池天数/在池增量（基线 = 7 天前快照）
    total_call = _calls_by_dim(fake, "total")[0]
    assert total_call["delta"] is None and total_call["stars"] is None  # 总星维度无增量/不引用数字
    summary_call = fake.summarize_calls[0]
    assert summary_call["full_name"] == "a/py-ai"
    assert summary_call["description"] == "译文-ai toolkit"  # 概要输入优先中文简介
    assert summary_call["language"] == "Python" and summary_call["readme"] is None  # 无 README 退化输入


def test_same_week_rerun_idempotent(conn):
    """同周跑两次幂等：第二轮翻译/推荐/概要全跳过（已译＋同维度同期已存在＋概要已生成），
    零 API 调用、库内零变化。"""
    _add_repo(conn, "a/one", description_en="first project")
    fake = FakeDeepSeekClient()
    stats1 = _run_ensure(conn, fake)
    assert stats1["translated"] == 1 and stats1["recommended"] == 3  # T-018：周＋季新区＋总星
    assert stats1["summarized"] == 1  # T-024：概要 1 条
    calls_after_first = (len(fake.translate_calls), len(fake.recommend_calls), len(fake.summarize_calls))
    stats2 = _run_ensure(conn, fake)
    assert stats2["listed"] == 1  # 覆盖集照算
    assert stats2["translated"] == 0 and stats2["recommended"] == 0 and stats2["summarized"] == 0
    assert (len(fake.translate_calls), len(fake.recommend_calls), len(fake.summarize_calls)) == calls_after_first
    assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 4  # 3 推荐语＋1 概要


def test_new_week_regenerates_recommendation(conn):
    """跨周生成新行（每周重新生成）：新周标签 INSERT，历史周保留；总星文本懒口径不重刷；已译不重译。"""
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
    assert stats2["listed"] == 1 and stats2["recommended"] == 2  # T-018：新周行 1 ＋ 季新区行 REPLACE 1
    assert stats2["translated"] == 0
    weeks = {
        r["period_label"]
        for r in conn.execute(
            "SELECT period_label FROM recommendations WHERE repo_id = ? AND dimension = 'week'", (repo_id,)
        )
    }
    assert weeks == {WEEK1, WEEK2}
    totals = conn.execute(
        "SELECT period_label FROM recommendations WHERE repo_id = ? AND dimension = 'total'", (repo_id,)
    ).fetchall()
    assert len(totals) == 1 and totals[0]["period_label"] == "all"  # 总星懒口径：跨周不新增不重刷


def test_translate_scope_excludes_unlisted_and_dead(conn):
    """T-017 范围收窄（v3 全池口径作废）：不占任何 Top50 的仓与 dead 仓不在 S → 不译不生成。

    T-028：默认 top_n 30→50，占榜构造同步扩到 51 个 filler（51 个星数更高者占满 total Top50，
    周榜 delta=10 并列按星数也占满 Top50）——a/off 星数最低仍不占任何榜。
    """
    _add_repo(conn, "a/listed", description_en="on board")
    # 51 个星数更高的仓占满周/总星两口径 Top50：a/off 星数最低 → 不占任何榜（total 榜无缺席概念，
    # "未上榜"必须靠占榜构造）
    for i in range(51):
        _add_repo(conn, f"f/fill{i:02d}", description_en=f"filler {i}", base=2000 + i * 10, delta=10)
    _add_repo(conn, "a/off", description_en="off board", base=100, delta=10)
    dead_id = _add_repo(conn, "a/dead", description_en="dead repo", base=3000, delta=10)
    conn.execute("UPDATE repos SET dead = 1 WHERE id = ?", (dead_id,))
    conn.commit()
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats["listed"] == 51  # S = 50 filler 占周/总星两口径 Top50 ＋ a/listed（周榜 delta 100 居首）
    # 范围收窄：a/off（不占任何榜）与 a/dead（dead 剔除）不送译
    assert "off board" not in fake.translate_calls
    assert "dead repo" not in fake.translate_calls
    zh = _zh_map(conn)
    assert zh["a/off"] is None and zh["a/dead"] is None
    # 推荐语也只给 S：a/off 与 a/dead 无任何维度行
    assert "a/off" not in _recommend_names(conn)
    assert "a/dead" not in _recommend_names(conn)
    assert "a/listed" in _recommend_names(conn)


def test_followed_unlisted_repo_gets_recommendations(conn):
    """关注仓不在主榜（单张快照无窗口）→ 周/季走新区（T-018：缺席仓属上榜口径，入池语境）＋总星维度
    （关注页长期盯梢语境，§8.1）。"""
    _add_repo(conn, "a/listed", description_en="on board")
    _add_repo(conn, "f/watched", description_en="watched but quiet", listed=False)
    conn.execute(
        "INSERT INTO follows (repo_id, created_at) VALUES ((SELECT id FROM repos WHERE full_name = 'f/watched'), ?)",
        ("2026-07-02T00:00:00Z",),
    )
    conn.commit()
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats["listed"] == 2  # S = 上榜 ∪ 关注
    assert stats["translated"] == 2  # 关注仓也翻译（范围内）
    dims_for_watched = {c["dimension"] for c in fake.recommend_calls if c["full_name"] == "f/watched"}
    assert dims_for_watched == {"week", "quarter", "total"}  # T-018：单张快照 → 周/季新区（入池语境）＋总星
    for dim in ("week", "quarter"):
        call = next(c for c in fake.recommend_calls if c["full_name"] == "f/watched" and c["dimension"] == dim)
        assert call["pool_days"] == 0.0 and call["delta"] == 0  # 单张快照：在池 0 天、在池增量 0
    rows = _recommend_rows(conn)
    assert ("f/watched", "total", "all", "推荐语-f/watched-total") in rows
    assert ("a/listed", "week", WEEK1, "推荐语-a/listed-week") in rows


def test_single_item_failure_degrades(conn, caplog):
    """单条失败降级：ensure 不抛、记 WARNING、其余项目正常、失败计入统计；翻译失败不连坐推荐；
    T-024：推荐语失败不连坐概要（summary 段独立计数）。"""
    _add_repo(conn, "a/bad-translate", description_en="boom translate")
    _add_repo(conn, "a/bad-recommend", description_en="fine desc")
    _add_repo(conn, "a/good", description_en="good desc")
    fake = FakeDeepSeekClient(fail_translate_for={"boom translate"}, fail_recommend_for={"a/bad-recommend"})
    with caplog.at_level(logging.WARNING, logger="app.ai"):
        stats = _run_ensure(conn, fake)  # 不抛出即通过
    assert stats["translate_failed"] == 1 and stats["translated"] == 2
    assert stats["recommend_failed"] == 3 and stats["recommended"] == 6  # T-018：a/bad-recommend 周＋季新区＋总星各失败 1 条
    assert stats["summarized"] == 3 and stats["summary_failed"] == 0  # T-024：概要不受推荐语失败连坐
    zh = _zh_map(conn)
    assert zh["a/bad-translate"] is None  # 翻译失败不留半成品
    assert zh["a/bad-recommend"] == "译文-fine desc" and zh["a/good"] == "译文-good desc"
    # 两段独立：a/bad-translate 翻译失败仍用英文原文生成推荐语；a/bad-recommend 三维度都无推荐语
    # 但概要独立生成成功（三仓 summary 行都在）
    assert _recommend_names(conn) == {"a/bad-translate", "a/bad-recommend", "a/good"}
    summary_names = {
        r["full_name"]
        for r in conn.execute(
            "SELECT r.full_name FROM recommendations c JOIN repos r ON r.id = c.repo_id"
            " WHERE c.dimension = 'summary'"
        )
    }
    assert summary_names == {"a/bad-translate", "a/bad-recommend", "a/good"}
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AI 翻译失败，跳过 a/bad-translate" in m for m in messages)
    assert any("AI 推荐语生成失败，跳过 a/bad-recommend" in m for m in messages)


def test_empty_key_returns_zero_stats(conn, monkeypatch):
    """降级：DEEPSEEK_API_KEY 为空 → 返回零统计、零 API 调用、库内零变化、不抛。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    _add_repo(conn, "a/one", description_en="first project")
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats == {
        "listed": 0,
        "translated": 0,
        "translate_failed": 0,
        "recommended": 0,
        "recommend_failed": 0,
        "summarized": 0,
        "summary_failed": 0,
        "readme_fetched": 0,
    }
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
    """断点续跑收敛（评审低-2）：第一轮推荐失败翻译成功（概要独立生成成功）→ 第二轮零翻译调用
    （已译跳过）、推荐语补齐、概要不重生成。"""
    _add_repo(conn, "a/one", description_en="first project")
    stats1 = _run_ensure(conn, FakeDeepSeekClient(fail_recommend_for={"a/one"}))
    assert stats1["translated"] == 1 and stats1["recommend_failed"] == 3  # T-018：周＋季新区＋总星各失败 1 条
    assert stats1["summarized"] == 1  # T-024：概要不随推荐语失败（独立段）
    assert _recommend_names(conn) == {"a/one"}  # 仅有概要行落库（三维度推荐语全失败）
    fake2 = FakeDeepSeekClient()
    stats2 = _run_ensure(conn, fake2)
    assert fake2.translate_calls == []  # 已译跳过：零翻译调用
    assert stats2["recommended"] == 3
    assert stats2["summarized"] == 0 and fake2.summarize_calls == []  # 概要已存在：幂等不重生成
    assert _recommend_names(conn) == {"a/one"}


def test_partial_failure_converges_next_run_translate(conn):
    """断点续跑收敛（评审低-2）：第一轮翻译失败推荐成功 → 第二轮只重译该条、同维度同期已存在推荐不重生成。"""
    _add_repo(conn, "a/one", description_en="first project")
    stats1 = _run_ensure(conn, FakeDeepSeekClient(fail_translate_for={"first project"}))
    assert stats1["translate_failed"] == 1 and stats1["recommended"] == 3  # T-018：周＋季新区＋总星；翻译失败不连坐推荐
    fake2 = FakeDeepSeekClient()
    stats2 = _run_ensure(conn, fake2)
    assert fake2.translate_calls == ["first project"]  # 只重译该条
    assert stats2["recommended"] == 0 and fake2.recommend_calls == []  # 周/总星行已存在：推荐零调用
    assert _zh_map(conn)["a/one"] == "译文-first project"


def test_quarter_dimension_generated_and_weekly_republish(conn):
    """季维度（86~94 天窗口快照 → 季榜出席）：首轮生成；同季次周 ensure 因其 generated_week ≠ 当周
    → REPLACE 重生（季内每周重生）；周/总星行不受影响。"""
    repo_id = _add_repo(conn, "a/q", description_en="quarter repo", quarter=True)
    fake = FakeDeepSeekClient()
    stats1 = _run_ensure(conn, fake)
    assert stats1["recommended"] == 3  # 周＋季＋总星
    rows1 = _recommend_rows(conn)
    assert ("a/q", "quarter", QUARTER1, "推荐语-a/q-quarter") in rows1
    q_call = _calls_by_dim(fake, "quarter")[0]
    assert q_call["delta"] == 100 and q_call["stars"] == 1100  # 季维度带本季增量语境
    # 次周（同季内）：quarter 行 generated_week ≠ 当周 → REPLACE 重生；周维度生成新周行；总星懒口径不动
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
        (repo_id, _iso(AS_OF_DT + timedelta(days=7)), 1600),
    )
    conn.commit()
    fake2 = FakeDeepSeekClient()
    stats2 = _run_ensure(conn, fake2, now=AS_OF_DT + timedelta(days=7))
    assert stats2["recommended"] == 2  # 季 REPLACE 1 ＋ 新周 1；总星懒口径跳过
    rows2 = _recommend_rows(conn)
    assert ("a/q", "quarter", QUARTER1, "推荐语-a/q-quarter") in rows2  # 仍同季标签，文本 REPLACE
    assert ("a/q", "week", WEEK2, "推荐语-a/q-week") in rows2
    assert len([r for r in rows2 if r[1] == "quarter"]) == 1  # REPLACE 不累积行
    # 同季同周再跑：季行 generated_week == 当周 → 全跳过
    stats3 = _run_ensure(conn, FakeDeepSeekClient(), now=AS_OF_DT + timedelta(days=7))
    assert stats3["recommended"] == 0


def test_has_cjk_detection():
    """CJK 判定：纯英文/空串 False，含任一汉字（含中英混排）True。"""
    assert has_cjk("pure english text") is False
    assert has_cjk("") is False
    assert has_cjk("已有中文") is True
    assert has_cjk("web 框架") is True


# ---------- T-017：README 输入与变更触发（全部 fake client，离线） ----------


def test_readme_used_in_recommend_input_and_stored_sha(conn):
    """README 正文截断入 prompt（周/总星两维度输入都带）；total 行落 readme_sha；计数进 stats。"""
    _add_repo(conn, "a/one", description_en="first project")
    readme_text = "# First\n\n" + "x" * 10000  # 超截断上限
    fake = FakeDeepSeekClient()
    github = FakeGitHubClient(readmes={"a/one": (readme_text, "sha-abc")})
    stats = _run_ensure(conn, fake, github_client=github)
    assert stats["readme_fetched"] == 1
    for call in fake.recommend_calls:
        assert call["readme"] == "# First\n\n" + "x" * (8000 - 9)  # 截断到 README_HEAD_CHARS
        assert "# First" in call["readme"]  # 保留 README 头部
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'total' AND period_label = 'all'"
    ).fetchone()
    assert row["readme_sha"] == "sha-abc"
    # 缓存命中：同轮同仓只拉一次
    assert github.readme_calls == ["a/one"]


def test_readme_sha_change_triggers_total_regeneration(conn):
    """README blob sha 变化 → 当日 ensure REPLACE 重生 ('total','all') 行并更新 sha；周行不受影响（不随 README 触发）。"""
    _add_repo(conn, "a/one", description_en="first project")
    github = FakeGitHubClient(readmes={"a/one": ("v1 readme", "sha-v1")})
    fake1 = FakeDeepSeekClient()
    _run_ensure(conn, fake1, github_client=github)
    rows1 = _recommend_rows(conn)
    assert ("a/one", "total", "all", "推荐语-a/one-total") in rows1
    week_count1 = len([r for r in rows1 if r[1] == "week"])

    # README 内容变化（sha 变）：total 重生（新文本），week 行保留原文本不重生成
    github.readmes["a/one"] = ("v2 readme", "sha-v2")
    fake2 = FakeDeepSeekClient()
    stats2 = _run_ensure(conn, fake2, github_client=github)
    assert stats2["recommended"] == 1  # 仅 total REPLACE
    assert stats2["translated"] == 0
    rows2 = _recommend_rows(conn)
    assert ("a/one", "total", "all", "推荐语-a/one-total") in rows2  # 文本同形（fake 回显），行数不变
    assert len([r for r in rows2 if r[1] == "total"]) == 1
    assert len([r for r in rows2 if r[1] == "week"]) == week_count1  # 周行未随 README 重生
    row = conn.execute(
        "SELECT readme_sha, generated_week FROM recommendations WHERE dimension = 'total'"
    ).fetchone()
    assert row["readme_sha"] == "sha-v2"

    # sha 未再变：第三轮全跳过（懒口径不重刷）
    stats3 = _run_ensure(conn, FakeDeepSeekClient(), github_client=github)
    assert stats3["recommended"] == 0


def test_readme_fetch_failure_keeps_existing_sha(conn):
    """F2-1 修复：已有 sha 指纹的行＋本次 README 拉取失败（sha 未取到）→ 不触发重生、指纹保留
    （防拉取失败制造每日 churn；下次拉取成功且 sha 变化才重生）。"""
    _add_repo(conn, "a/one", description_en="first project")
    github = FakeGitHubClient(readmes={"a/one": ("v1 readme", "sha-v1")})
    _run_ensure(conn, FakeDeepSeekClient(), github_client=github)
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'total'"
    ).fetchone()
    assert row["readme_sha"] == "sha-v1"

    # 本次拉取抛普通异常 → sha=None：不重生、旧指纹保留
    github.fail_for = {"a/one"}
    fake2 = FakeDeepSeekClient()
    stats2 = _run_ensure(conn, fake2, github_client=github)
    assert stats2["recommended"] == 0  # 不重生（week 行已存在、total 未触发）
    assert fake2.recommend_calls == []
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'total'"
    ).fetchone()
    assert row["readme_sha"] == "sha-v1"


def test_readme_auth_failure_keeps_existing_sha(conn):
    """F2-1 修复：已有 sha 指纹的行＋GitHubAuthError（确定性停拉）→ 不重生、指纹保留。"""
    _add_repo(conn, "a/one", description_en="first project")
    github = FakeGitHubClient(readmes={"a/one": ("v1 readme", "sha-v1")})
    _run_ensure(conn, FakeDeepSeekClient(), github_client=github)

    github.auth_for = {"a/one"}  # 之后拉取抛账户类错误 → 停拉降级
    fake2 = FakeDeepSeekClient()
    stats2 = _run_ensure(conn, fake2, github_client=github)
    assert stats2["recommended"] == 0
    assert fake2.recommend_calls == []
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'total'"
    ).fetchone()
    assert row["readme_sha"] == "sha-v1"


def test_readme_null_sha_self_heals_on_first_appearance(conn):
    """sha NULL 自愈：首轮无 README（sha=NULL 落库）→ 后续出现 README（sha 非空）→ 视为变更当日重生。"""
    _add_repo(conn, "a/one", description_en="first project")
    github = FakeGitHubClient(readmes={"a/one": (None, None)})  # 无 README（404 退化）
    fake1 = FakeDeepSeekClient()
    _run_ensure(conn, fake1, github_client=github)
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'total'"
    ).fetchone()
    assert row["readme_sha"] is None

    # 后续 README 出现（sha 非空）→ 自愈：total 重生并更新 sha
    github.readmes["a/one"] = ("now has readme", "sha-new")
    stats2 = _run_ensure(conn, FakeDeepSeekClient(), github_client=github)
    assert stats2["recommended"] == 1
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'total'"
    ).fetchone()
    assert row["readme_sha"] == "sha-new"


def test_readme_fetch_failure_degrades_without_blocking(conn, caplog):
    """README 拉取失败 → 推荐语照常生成（退化元数据输入，readme=None 入 prompt）、readme_sha=NULL、不抛出。"""
    _add_repo(conn, "a/one", description_en="first project")
    github = FakeGitHubClient(fail_for={"a/one"})
    fake = FakeDeepSeekClient()
    with caplog.at_level(logging.WARNING, logger="app.ai"):
        stats = _run_ensure(conn, fake, github_client=github)  # 不抛出即通过
    assert stats["recommended"] == 3  # T-018：周＋季新区＋总星
    assert all(c["readme"] is None for c in fake.recommend_calls)
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'total'"
    ).fetchone()
    assert row["readme_sha"] is None
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("README 拉取失败" in m for m in messages)


def test_readme_auth_error_stops_fetching_but_recommend_continues(conn, caplog):
    """GitHub token 无效（确定性账户类错误）：首次拉取后停拉全量退化，推荐语照常生成，readme_sha 保持 NULL。"""
    _add_repo(conn, "a/one", description_en="first project")
    _add_repo(conn, "a/two", description_en="second project")
    github = FakeGitHubClient(auth_for={"a/one"})  # 第一个仓即 auth 错误 → 停拉
    with caplog.at_level(logging.WARNING, logger="app.ai"):
        stats = _run_ensure(conn, FakeDeepSeekClient(), github_client=github)  # 不抛出即通过
    assert stats["recommended"] == 6  # T-018：2 仓 × 3 维度照常生成
    assert github.readme_calls == ["a/one"]  # 第二个仓不再尝试（auth 停拉）
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE readme_sha IS NOT NULL").fetchone()[0] == 0
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("GitHub README 拉取终止（账户类错误）" in m for m in messages)


def test_no_github_client_degrades_readme_silently(conn):
    """jobs 未接线 GitHub client（或 token 缺失路径）：README 全量退化（NULL sha），推荐语照常，不报错。"""
    _add_repo(conn, "a/one", description_en="first project")
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)  # github_client 缺省 None
    assert stats["recommended"] == 3  # T-018：周＋季新区＋总星
    assert all(c["readme"] is None for c in fake.recommend_calls)
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'total'"
    ).fetchone()
    assert row["readme_sha"] is None


# ---------- T-024：AI 概要（§11；全部 fake client，离线） ----------


def test_client_summarize_prompt_shape():
    """summarize prompt 钉死口径：README 文档视角"是什么/由什么组成"（§11.1 冻结文本逐字，评审 F3-1 对齐）、
    段落级 3~5 句、不引用任何数字、只输出概要本体（无前缀/列表/标题）、低温度 0.3；输入含 README 截断文本。"""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "概要本体"}}]})

    client = DeepSeekClient("test-key", transport=httpx.MockTransport(handler))
    asyncio.run(client.summarize(full_name="a/one", description="desc", language="Python", readme="readme body"))
    asyncio.run(client.aclose())

    system, user = bodies[0]["messages"][0]["content"], bodies[0]["messages"][1]["content"]
    assert "3~5 句" in system  # 段落级句数要求
    assert "这个项目是什么" in system and "由什么组成" in system  # README 文档视角（无维度概念，§11.1 冻结文本逐字）
    assert "怎么用" not in system  # F3-1：冻结文本只钉"是什么/怎么组成"，不扩口径
    assert "不要引用任何具体数字" in system  # 与 total 推荐语同口径：不引用数字（星数/增星/排名均为禁令列举）
    assert "周榜" not in system and "季榜" not in system and "总星" not in system  # 无维度概念：与三维度推荐语可辨
    assert "只输出概要本体" in system and "不要用列表或标题" in system  # 同 recommend 风格约束
    assert bodies[0]["temperature"] == 0.3
    assert "仓库：a/one" in user and "简介：desc" in user and "主语言：Python" in user
    assert "README 要点：\nreadme body" in user
    assert "categories" not in user and "星数" not in user  # 概要输入不带分类/星数


def test_summary_skipped_when_refresh_false(conn):
    """§11 无手动入口：refresh=False（手动批量 worker 路径）summary 段整段跳过——零 summarize 调用、
    库内无 summary 行；推荐语补缺照常。"""
    _add_repo(conn, "a/one", description_en="first project")
    fake = FakeDeepSeekClient()
    stats = asyncio.run(recommend_missing(conn, fake, now=AS_OF_DT, refresh=False))
    assert stats["summarized"] == 0 and stats["summary_failed"] == 0
    assert fake.summarize_calls == []
    assert stats["recommended"] == 3  # 三维度推荐语照常补缺
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE dimension = 'summary'").fetchone()[0] == 0


def test_summary_generated_for_scope_and_idempotent(conn):
    """覆盖 S 全集（上榜∪关注）：缺失生成 ('summary','all') 行（period_label 恒 'all'、generated_week 当周）；
    同轮同仓只生成一条（existing 防御）；次轮幂等零调用。"""
    _add_repo(conn, "a/one", description_en="first project")
    _add_repo(conn, "f/watched", description_en="watched but quiet", listed=False)
    conn.execute(
        "INSERT INTO follows (repo_id, created_at) VALUES ((SELECT id FROM repos WHERE full_name = 'f/watched'), ?)",
        ("2026-07-02T00:00:00Z",),
    )
    conn.commit()
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats["summarized"] == 2 and stats["summary_failed"] == 0
    assert [c["full_name"] for c in fake.summarize_calls] == ["f/watched", "a/one"]  # all_names 序：关注集在前、三榜去重在后（每仓恰好一条，existing 防御）
    rows = conn.execute(
        "SELECT r.full_name, c.period_label, c.generated_week FROM recommendations c"
        " JOIN repos r ON r.id = c.repo_id WHERE c.dimension = 'summary' ORDER BY r.full_name"
    ).fetchall()
    assert [(r["full_name"], r["period_label"], r["generated_week"]) for r in rows] == [
        ("a/one", "all", WEEK1),
        ("f/watched", "all", WEEK1),
    ]
    fake2 = FakeDeepSeekClient()
    stats2 = _run_ensure(conn, fake2)
    assert stats2["summarized"] == 0 and fake2.summarize_calls == []  # 已生成：幂等不重刷


def test_summary_sha_change_regenerates(conn):
    """README blob sha 变化 → summary 行 REPLACE 重生并更新 sha（README 变更当日重生，懒口径不每周重刷）。"""
    _add_repo(conn, "a/one", description_en="first project")
    github = FakeGitHubClient(readmes={"a/one": ("v1 readme", "sha-v1")})
    fake1 = FakeDeepSeekClient()
    _run_ensure(conn, fake1, github_client=github)
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'summary'"
    ).fetchone()
    assert row["readme_sha"] == "sha-v1"

    github.readmes["a/one"] = ("v2 readme", "sha-v2")
    fake2 = FakeDeepSeekClient()
    stats2 = _run_ensure(conn, fake2, github_client=github)
    assert stats2["summarized"] == 1  # 仅概要 REPLACE 重生
    assert stats2["recommended"] == 1  # total 段同触发（README 变更当日 total 也重生）
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE dimension = 'summary'").fetchone()[0] == 1
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'summary'"
    ).fetchone()
    assert row["readme_sha"] == "sha-v2"

    stats3 = _run_ensure(conn, FakeDeepSeekClient(), github_client=github)
    assert stats3["summarized"] == 0  # sha 未再变：懒口径不重刷


def test_summary_sha_none_keeps_existing(conn):
    """F2-1 守卫（与 total 段同款）：已有行＋本次 sha 未取到（拉取失败）→ 不触发重生、旧指纹保留。"""
    _add_repo(conn, "a/one", description_en="first project")
    github = FakeGitHubClient(readmes={"a/one": ("v1 readme", "sha-v1")})
    _run_ensure(conn, FakeDeepSeekClient(), github_client=github)
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'summary'"
    ).fetchone()
    assert row["readme_sha"] == "sha-v1"

    github.fail_for = {"a/one"}  # 本次拉取抛普通异常 → sha=None
    fake2 = FakeDeepSeekClient()
    stats2 = _run_ensure(conn, fake2, github_client=github)
    assert stats2["summarized"] == 0 and fake2.summarize_calls == []
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'summary'"
    ).fetchone()
    assert row["readme_sha"] == "sha-v1"


def test_summary_single_failure_counts_and_continues(conn, caplog):
    """单条概要失败：记 WARNING 跳过计入 summary_failed、其余仓正常；不连坐推荐语段。"""
    _add_repo(conn, "a/bad", description_en="bad desc")
    _add_repo(conn, "a/good", description_en="good desc")
    fake = FakeDeepSeekClient(fail_summarize_for={"a/bad"})
    with caplog.at_level(logging.WARNING, logger="app.ai"):
        stats = _run_ensure(conn, fake)  # 不抛出即通过
    assert stats["summarized"] == 1 and stats["summary_failed"] == 1
    assert stats["recommended"] == 6  # 推荐语段不受概要失败影响
    summary_names = {
        r["full_name"]
        for r in conn.execute(
            "SELECT r.full_name FROM recommendations c JOIN repos r ON r.id = c.repo_id"
            " WHERE c.dimension = 'summary'"
        )
    }
    assert summary_names == {"a/good"}  # a/bad 无概要行（不留半成品）
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AI 概要生成失败，跳过 a/bad" in m for m in messages)


def test_summary_auth_error_passes_through(conn):
    """DeepSeekAuthError 直通契约（同推荐语段）：概要段不吞账户类错误——整轮抛出、当条不落库。"""
    _add_repo(conn, "a/one", description_en="first project")

    class AuthSummarizeClient(FakeDeepSeekClient):
        async def summarize(self, **kwargs):
            raise DeepSeekAuthError("模拟 key 无效")

    with pytest.raises(DeepSeekAuthError):
        _run_ensure(conn, AuthSummarizeClient())
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE dimension = 'summary'").fetchone()[0] == 0


def test_summary_reuses_readme_state_cache(conn):
    """README 复用同一 _ReadmeState 实例：total 段与 summary 段同轮同仓只拉一次（逐仓缓存命中）；
    summary 输入带 README 截断文本（同一 README_HEAD_CHARS 截断约定）；readme_sha 落库。"""
    _add_repo(conn, "a/one", description_en="first project")
    readme_text = "# First\n\n" + "x" * 10000  # 超截断上限
    github = FakeGitHubClient(readmes={"a/one": (readme_text, "sha-abc")})
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake, github_client=github)
    assert stats["readme_fetched"] == 1
    assert github.readme_calls == ["a/one"]  # 缓存复用：三维度＋概要同轮只拉一次
    assert fake.summarize_calls[0]["readme"] == "# First\n\n" + "x" * (8000 - 9)  # 与 recommend 同一截断口径
    row = conn.execute(
        "SELECT readme_sha FROM recommendations WHERE dimension = 'summary'"
    ).fetchone()
    assert row["readme_sha"] == "sha-abc"


# ---------- T-018：新区仓进覆盖集（入池语境 prompt 分化；全部 fake client，离线） ----------


def test_rising_repo_in_scope_with_pool_context(conn):
    """T-018 覆盖集：缺席仓（跨度不足）进周/季榜集——推荐语调用携带入池语境（pool_days＋在池增量）；
    主榜仓不携带（pool_days=None）。"""
    _add_repo(conn, "a/attending", description_en="attending repo")
    rid = _add_repo(conn, "a/rising", description_en="rising repo", listed=False)
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
        (rid, _iso(AS_OF_DT - timedelta(days=3)), 500),
    )
    conn.commit()
    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats["listed"] == 2
    week_call = next(c for c in _calls_by_dim(fake, "week") if c["full_name"] == "a/rising")
    assert week_call["pool_days"] == 3.0 and week_call["delta"] == 600  # 在池增量 = 端点 − 最旧基线
    assert week_call["stars"] == 1100
    q_call = next(c for c in _calls_by_dim(fake, "quarter") if c["full_name"] == "a/rising")
    assert q_call["pool_days"] == 3.0 and q_call["delta"] == 600
    main_call = next(c for c in _calls_by_dim(fake, "week") if c["full_name"] == "a/attending")
    assert main_call["pool_days"] is None  # 主榜仓不带入池语境（prompt 措辞一字不动）


def test_scope_sets_include_rising_rows(conn):
    """T-018 _scope_sets 扩展：周/季榜集 = 主榜 Top30 ∪ 新区 Top10——缺席仓带 rising 语境入集；
    总星榜集不动（无新区概念，缺席仓以主榜行形态入集）。"""
    from app.ai import _scope_sets

    _add_repo(conn, "a/attending", description_en="attending repo")
    _add_repo(conn, "a/rising", description_en="rising repo", listed=False)
    listed_by_period, follow_names = _scope_sets(conn, now=AS_OF_DT)
    assert follow_names == []
    week_item = listed_by_period["week"]["a/rising"]
    assert week_item.row is None and week_item.rising is not None
    assert week_item.rising.pool_delta == 0 and week_item.rising.pool_days == 0.0  # 单张快照：在池 0 天
    main_item = listed_by_period["week"]["a/attending"]
    assert main_item.row is not None and main_item.rising is None
    assert "a/rising" in listed_by_period["quarter"]
    total_item = listed_by_period["total"]["a/rising"]
    assert total_item.row is not None and total_item.rising is None  # 总星榜集不动：新区概念不存在


def test_fresh_zone_repo_in_scope_with_main_row_treatment(conn):
    """T-033 覆盖集 S 扩展：仅因新项目区上榜的仓（出席、创建 < 1 年、不在主榜 Top50）进三口径榜集——
    与主榜行同待遇（row 非 None、窗口增量语境 pool_days=None）；翻译/推荐语/概要补齐（规格 Scenario）。"""
    # 51 个星数更高者占满周/总星两口径主榜 Top50：a/fresh 仅凭新项目区上榜
    for i in range(51):
        _add_repo(conn, f"f/fill{i:02d}", description_en=f"filler {i}", base=2000 + i * 10, delta=10)
    _add_repo(conn, "a/fresh", description_en="fresh project", base=100, delta=10, github_created_at="2026-01-01T00:00:00Z")

    from app.ai import _scope_sets

    listed_by_period, _ = _scope_sets(conn, now=AS_OF_DT)
    fresh_item = listed_by_period["week"]["a/fresh"]
    assert fresh_item.row is not None and fresh_item.rising is None  # 与主榜行同待遇（row 非 None）
    assert "a/fresh" in listed_by_period["total"]  # total 口径新项目区上榜入集
    # quarter：a/fresh 缺席（无 90 天快照）且被 51 个并列在池增量仓挤出 rising Top10 → 不在季集
    # （上榜资格由榜单计算决定，S 集只是榜单的投影）
    assert "a/fresh" not in listed_by_period["quarter"]

    fake = FakeDeepSeekClient()
    stats = _run_ensure(conn, fake)
    assert stats["listed"] == 51  # 50 filler（周/总星主榜 Top50）＋ a/fresh（新项目区）；星最低的 filler 未上榜不在 S
    week_call = next(c for c in _calls_by_dim(fake, "week") if c["full_name"] == "a/fresh")
    assert week_call["pool_days"] is None  # 新项目区行不带入池语境（与主榜行同待遇，§16.1）
    assert week_call["delta"] == 10 and week_call["stars"] == 110  # 窗口增量语境
    assert "fresh project" in fake.translate_calls  # 翻译覆盖（S 内）
    rows = _recommend_rows(conn)
    assert ("a/fresh", "total", "all", "推荐语-a/fresh-total") in rows  # 推荐语覆盖（总星维度）
    assert any(c["full_name"] == "a/fresh" for c in fake.summarize_calls)  # 概要覆盖


def test_client_recommend_rising_pool_context():
    """新区仓 prompt 分化（T-018）：输入行"入池 N 天新增星数：X"＋钉死句"必须明确写入池 N 天新增星数（X 星）"；
    主榜仓措辞一字不动（"本周新增星数"/"必须明确写出本周新增星数"照旧）。"""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "推荐语本体"}}]})

    client = DeepSeekClient("test-key", transport=httpx.MockTransport(handler))
    base = {"full_name": "a/one", "description": "desc", "language": "Python", "categories": ["Python"]}
    asyncio.run(client.recommend(**base, dimension="week", delta=600, stars=1100, pool_days=3.0))
    asyncio.run(client.recommend(**base, dimension="week", delta=150, stars=1100))
    asyncio.run(client.aclose())

    rising_user, rising_sys = bodies[0]["messages"][1]["content"], bodies[0]["messages"][0]["content"]
    assert "入池 3 天新增星数：600" in rising_user
    assert "必须明确写入池 3 天新增星数（600 星）" in rising_sys
    assert "本周新增星数" not in rising_user and "写出本周" not in rising_sys
    main_user, main_sys = bodies[1]["messages"][1]["content"], bodies[1]["messages"][0]["content"]
    assert "本周新增星数：150" in main_user
    assert "必须明确写出本周新增星数（150 星）" in main_sys


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
    """ensure_daily_ai 整轮抛错：daily_job 仍正常完成、记 ERROR 不抛出；AI 在 run_daily 之后串行。"""
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = []

    async def fake_run_daily(client, conn, *, log=None):
        calls.append("run_daily")

    async def fake_ensure(conn, client, *, now, log=None, github_client=None):
        calls.append("ensure")
        raise RuntimeError("模拟 AI 整轮失败")

    monkeypatch.setattr("app.jobs.GitHubClient", _FakeGitHubClient)
    monkeypatch.setattr("app.jobs.run_daily", fake_run_daily)
    monkeypatch.setattr("app.jobs.ensure_daily_ai", fake_ensure)
    # get_job_logger 落文件且 propagate=False：换成可传播的测试 logger 供 caplog 断言
    monkeypatch.setattr("app.jobs.get_job_logger", lambda: logging.getLogger("test-daily-job"))

    with caplog.at_level(logging.ERROR, logger="test-daily-job"):
        asyncio.run(daily_job())  # 不抛出即通过
    assert calls == ["run_daily", "ensure"]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("AI 每日生成整轮异常" in m for m in errors)


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

    asyncio.run(daily_job())  # 真 ensure_daily_ai：key 空 → 记 INFO 返回零统计
    assert calls == ["run_daily"]
    conn = get_conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 0
    finally:
        conn.close()
