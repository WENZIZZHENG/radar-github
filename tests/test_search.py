"""T-034→T-036 智能搜索测试：检索链路（app.search）＋ P8 页面/路由（TestClient + 假 AI/GitHub client），全程离线禁真打 API。

覆盖任务书口径（spec smart-search 钉死）：
- 意图理解：JSON 契约解析、语言归一/非法值丢弃、非法 JSON/坏形态退化原输入单关键词、多关键词多语言产出；
  追问时携带旧意图合并、合并非法退化为本轮新输入单关键词（不沿用旧意图）；
- 池内召回：命中计数（字段×关键词）、语言过滤交集、粗排（命中数→星数）、dead 排除、topics 命中、不足 50/零候选；
- 精选：正常编号+理由（LLM 顺序保持）、空 results 不凑数、解析失败/超范围编号/理由非法/调用失败 → 粗排直出无理由
  （WARNING）、AuthError 直通；
- 池外补搜：补足到 20、池内在前池外在后、已在池排除、两次调用补不满按实际、失败/限速/token 缺失跳过补搜（池内照常）；
- 降级：DeepSeek 不可用 → 页面"搜索暂不可用"，主链路不受影响；
- 页面：顶栏 6 项、意图透明行、池外徽标、空结果如实说明、追问搜空保留上一轮结果、池外行关注星/已在池不显示关注入口
  （模板分支）、池内行 created_year（T-033 红线：搜索行视图必须传该字段）。

异步用例统一 asyncio.run 驱动（不引 pytest-asyncio，与 test_follows/test_ai 同口径）。
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.ai import DeepSeekAuthError, DeepSeekError
from app.collector.github import GitHubAuthError, GitHubError
from app.db import get_conn, init_db
from app.main import app
from app.search import (
    RESULT_LIMIT,
    Intent,
    _build_intent,
    _github_query,
    _select_with_ai,
    recall_candidates,
    run_search,
    search_external,
)
from app.web.routes import _ai_client, _github_client, templates

# ---------- 工具：假 client 与种子 ----------


class FakeSearchAI:
    """假 DeepSeek client：understand_intent / select_and_reason 按脚本返回。

    intent：dict（正常）/ None（内容非法 JSON）；intent_error：异常（调用失败/Auth，正常时 None）；
    select：dict（正常）/ None（解析失败）；select_error：异常。记录调用供断言。
    intent_thinking/select_thinking（T-041 流式）：非空时按段喂 thinking_cb 并记录
    thinking_calls（"stage:文本" 序列），模拟真 client 的流式思维链下发。
    """

    def __init__(
        self,
        intent=None,
        select=None,
        intent_error=None,
        select_error=None,
        intent_thinking=(),
        select_thinking=(),
    ):
        self._intent = intent
        self._select = select
        self._intent_error = intent_error
        self._select_error = select_error
        self._intent_thinking = list(intent_thinking)
        self._select_thinking = list(select_thinking)
        self.intent_calls: list[str] = []
        self.select_calls: list[dict] = []
        self.thinking_calls: list[str] = []

    async def understand_intent(self, query: str, *, prev_intent=None, thinking_cb=None):
        self.intent_calls.append((query, prev_intent))
        if thinking_cb is not None:
            for delta in self._intent_thinking:
                thinking_cb(delta)
                self.thinking_calls.append("intent:" + delta)
        if self._intent_error is not None:
            raise self._intent_error
        return self._intent

    async def select_and_reason(self, *, intent_summary, candidates, thinking_cb=None):
        self.select_calls.append(candidates)
        if thinking_cb is not None:
            for delta in self._select_thinking:
                thinking_cb(delta)
                self.thinking_calls.append("select:" + delta)
        if self._select_error is not None:
            raise self._select_error
        return self._select


def make_search_item(full_name, *, description="external desc", language="Python", stars=5000, created_at="2024-01-01T00:00:00Z"):
    """造一条 GitHub Search item（字段与采集层同构，含 T-033 created_at 与关注入池必需的 node_id）。"""
    item = {
        "full_name": full_name,
        "node_id": f"node-{full_name}",
        "description": description,
        "language": language,
        "stargazers_count": stars,
        "topics": [],
    }
    if created_at is not None:
        item["created_at"] = created_at
    return item


class FakeSearchGitHub:
    """假 GitHub client：search_repositories 按页脚本返回；fetch_repo 供关注动态入池；error 模拟上游故障；记录调用。"""

    def __init__(self, pages=None, error=None, fetch_items=None):
        self.pages = pages or [[]]
        self.error = error
        self.fetch_items = fetch_items or {}
        self.calls: list[dict] = []

    async def search_repositories(self, query, *, sort="stars", per_page=100, page=1):
        self.calls.append({"query": query, "sort": sort, "per_page": per_page, "page": page})
        if self.error is not None:
            raise self.error
        if page > len(self.pages):
            return {"items": []}
        return {"items": self.pages[page - 1]}

    async def fetch_repo(self, full_name: str) -> dict:
        return self.fetch_items[full_name]


def _open_db(tmp_path):
    db = tmp_path / "search.db"
    init_db(db)
    return get_conn(db)


def _add_repo(
    conn,
    name,
    *,
    description_en="demo",
    language="Python",
    topics=(),
    dead=0,
    stars=100,
    github_created_at=None,
):
    """插一个池内仓库＋一张最新快照（created_at 固定历史日期，时间稳健）。"""
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, language, topics, dead, source, created_at, github_created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'test', '2026-07-01T00:00:00Z', ?)",
        (name, f"node-{name}", description_en, language, json.dumps(list(topics)), dead, github_created_at),
    )
    repo_id = cur.lastrowid
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, '2026-08-09T00:00:00Z', ?)",
        (repo_id, stars),
    )
    return repo_id


def _run_search(conn, ai, gh, query, *, prev_intent=None):
    return asyncio.run(run_search(conn, ai, gh, query, prev_intent=prev_intent))


# ---------- 4.1 意图理解：JSON 契约解析、非法 JSON 退化、多关键词/多语言产出 ----------


def test_intent_build_multi_keywords_languages_and_normalize():
    """多关键词×多语言产出：小写/去空格归一、语言大小写不敏感归一到 LANGUAGES 精确名、去重保序、主题词小写。"""
    intent = _build_intent(
        "我要做爬虫",
        {"keywords": [" Crawler ", "scraping", "crawler"], "languages": ["python", "Python"], "topics": ["AI", " Web-Scraping "]},
    )
    assert not intent.degraded
    assert intent.keywords == ["crawler", "scraping"]  # 去重保序
    assert intent.languages == ["Python"]  # 重复丢弃
    assert intent.topics == ["ai", "web-scraping"]


def test_intent_build_filters_invalid_languages():
    """语言取值对齐 classify.LANGUAGES 键集：非法语言名丢弃，全非法则语言为空（不过滤）。"""
    intent = _build_intent("x", {"keywords": ["k"], "languages": ["Cobol", "cpp"], "topics": []})
    assert intent.languages == []


def test_intent_build_normalizes_new_languages():
    """P8 新合法值（语言榜扩容）：C++/C# 等新增语言大小写不敏感归一到 GitHub 精确名，不再被丢弃。"""
    intent = _build_intent("x", {"keywords": ["k"], "languages": ["c++", "C#"], "topics": []})
    assert intent.languages == ["C++", "C#"]


def test_intent_build_degraded_on_bad_shapes():
    """非法 JSON 退化（spec：以原输入为单一关键词继续检索，不当场失败）：非对象/缺键/关键词空/元素非 str。"""
    bad_parsed = (
        None,
        "not a dict",
        {"keywords": None, "languages": [], "topics": []},
        {"keywords": [], "languages": [], "topics": []},
        {"keywords": [123], "languages": [], "topics": []},
        {"keywords": ["k"], "languages": "Python", "topics": []},
    )
    for parsed in bad_parsed:
        intent = _build_intent("我要做爬虫", parsed)
        assert intent.degraded
        assert intent.keywords == ["我要做爬虫"]  # 原输入作为单一关键词
        assert intent.languages == []


def test_run_search_degraded_on_invalid_intent_json(tmp_path):
    """意图理解返回内容非法（understand_intent → None）：退化为原输入单关键词继续池内检索，页面不报错。"""
    conn = _open_db(tmp_path)
    _add_repo(conn, "a/crawler", description_en="web crawler framework", stars=2000)
    conn.commit()
    ai = FakeSearchAI(intent=None, select={"results": [{"id": 1, "reason": "理由"}]})
    result = _run_search(conn, ai, FakeSearchGitHub(), "crawler")
    assert result.unavailable is False
    assert result.intent.degraded
    assert result.intent.keywords == ["crawler"]  # 原输入单关键词
    assert [h.candidate.full_name for h in result.pool_hits] == ["a/crawler"]
    assert result.pool_hits[0].reason == "理由"
    conn.close()


def test_run_search_follow_up_merge_intent(tmp_path):
    """追问意图合并：旧意图 + 新输入 → LLM 收到 prev_intent 并产出合并意图，以新意图重跑检索。"""
    conn = _open_db(tmp_path)
    _add_repo(conn, "a/crawler", description_en="web crawler framework", language="Python", stars=2000)
    _add_repo(conn, "a/go-crawler", description_en="go crawler framework", language="Go", stars=2000)
    conn.commit()
    old = Intent("我要做爬虫", ["crawler", "scraping"], ["Python"])
    merged = {"keywords": ["crawler", "scraping"], "languages": ["Go"], "topics": []}
    ai = FakeSearchAI(intent=merged, select={"results": [{"id": 2, "reason": "Go 版理由"}]})
    result = _run_search(conn, ai, FakeSearchGitHub(), "换成 Go 的", prev_intent=old)
    assert result.unavailable is False
    assert not result.intent.degraded
    assert result.intent.languages == ["Go"]
    assert [h.candidate.full_name for h in result.pool_hits] == ["a/go-crawler"]
    # 校验 LLM 收到旧意图（含 filters/unsupported）
    assert len(ai.intent_calls) == 1
    assert ai.intent_calls[0] == (
        "换成 Go 的",
        {"keywords": ["crawler", "scraping"], "languages": ["Python"], "topics": [], "filters": {}, "unsupported": []},
    )
    conn.close()


def test_run_search_follow_up_merge_invalid_degrades_current(tmp_path):
    """追问合并返回非法 JSON：以本轮新输入单关键词退化，不沿用旧意图。"""
    conn = _open_db(tmp_path)
    _add_repo(conn, "a/crawler", description_en="web crawler framework", language="Python", stars=2000)
    conn.commit()
    old = Intent("我要做爬虫", ["crawler", "scraping"], ["Python"])
    ai = FakeSearchAI(intent=None, select={"results": [{"id": 1, "reason": "理由"}]})
    result = _run_search(conn, ai, FakeSearchGitHub(), "只要异步的", prev_intent=old)
    assert result.intent.degraded
    assert result.intent.keywords == ["只要异步的"]  # 本轮新输入，非旧意图
    assert result.intent.languages == []
    conn.close()


def test_constants_t036():
    """T-036 数量口径：精选/补搜上限 20，候选粗排池 50。"""
    from app.search import POOL_TOP_N, RESULT_LIMIT

    assert RESULT_LIMIT == 20
    assert POOL_TOP_N == 50


# ---------- 4.2 池内召回：命中计数、语言过滤、粗排顺序、不足 50/零候选 ----------


def _seed_recall(conn):
    """五仓种子：a/multi 命中 3 次（desc 两关键词 + topics 一关键词）星 1000；a/single-py 命中 1 次星 900；
    a/single-go 命中 1 次星 3000（Go）；a/high 命中 1 次星 5000；a/dead 命中但 dead=1。"""
    _add_repo(conn, "a/multi", description_en="crawler and scraping framework", topics=["crawler"], language="Python", stars=1000)
    _add_repo(conn, "a/single-py", description_en="crawler tool", language="Python", stars=900)
    _add_repo(conn, "a/single-go", description_en="crawler tool", language="Go", stars=3000)
    _add_repo(conn, "a/high", description_en="crawler tool", language="Python", stars=5000)
    _add_repo(conn, "a/dead", description_en="crawler tool", language="Python", stars=9999, dead=1)
    conn.commit()


def test_recall_hit_count_and_rank_order(tmp_path):
    """命中计数（字段×关键词组合）与粗排：命中数降序 → 星数降序；dead 仓排除；语言过滤交集。"""
    conn = _open_db(tmp_path)
    _seed_recall(conn)
    cands = recall_candidates(conn, Intent("crawler", ["crawler", "scraping"], ["Python"]))
    assert [(c.full_name, c.hits) for c in cands] == [("a/multi", 3), ("a/high", 1), ("a/single-py", 1)]
    assert [c.stars for c in cands] == [1000, 5000, 900]  # 同命中数按星数降序；dead 与 Go 仓被排除
    conn.close()


def test_recall_without_language_filter(tmp_path):
    """语言数组为空 = 不过滤：全部命中仓都进（含 Go）；dead 恒排除。"""
    conn = _open_db(tmp_path)
    _seed_recall(conn)
    cands = recall_candidates(conn, Intent("crawler", ["crawler"]))
    assert {c.full_name for c in cands} == {"a/multi", "a/high", "a/single-py", "a/single-go"}
    assert all(c.full_name != "a/dead" for c in cands)
    conn.close()


def test_recall_topics_and_full_name_match(tmp_path):
    """topics（JSON 字符串）与 full_name 命中均计入召回。"""
    conn = _open_db(tmp_path)
    _add_repo(conn, "CrawlerKit/x", description_en="nothing here", topics=["ai"], language="Python", stars=100)
    _add_repo(conn, "a/tool", description_en="nothing here", topics=["crawler"], language="Python", stars=200)
    conn.commit()
    cands = recall_candidates(conn, Intent("c", ["crawler"]))
    assert {c.full_name for c in cands} == {"CrawlerKit/x", "a/tool"}  # 前者 full_name 命中、后者 topics 命中
    conn.close()


def test_recall_less_than_50_and_zero(tmp_path):
    """不足 50 按实际（不凑数）；零候选返回空列表；关键词全空返回空列表。"""
    conn = _open_db(tmp_path)
    _add_repo(conn, "a/one", description_en="crawler", stars=100)
    conn.commit()
    assert len(recall_candidates(conn, Intent("c", ["crawler"]))) == 1
    assert recall_candidates(conn, Intent("c", ["ghost-term"])) == []
    assert recall_candidates(conn, Intent("c", [])) == []
    conn.close()


# ---------- 4.3 精选：正常 20 条+理由、失败粗排兜底、超范围编号拒绝 ----------


def _seed_select(conn, n):
    """n 个命中仓（星数递减，粗排序 a/r00..a/r{n-1}）。"""
    for i in range(n):
        _add_repo(conn, f"a/r{i:02d}", description_en=f"crawler variant {i}", stars=1000 - i)
    conn.commit()


def test_select_normal_keeps_llm_order(tmp_path):
    """正常精选：编号引用 + 理由，输出顺序 = LLM 返回顺序（按匹配度排序），不足 20 条不凑数。"""
    conn = _open_db(tmp_path)
    _seed_select(conn, 5)
    base = {"keywords": ["crawler"], "languages": [], "topics": []}
    ai = FakeSearchAI(intent=base, select={"results": [{"id": 3, "reason": "理由三"}, {"id": 1, "reason": "理由一"}]})
    intent = _build_intent("crawler", base)
    hits = asyncio.run(_select_with_ai(ai, intent, recall_candidates(conn, intent)))
    assert [(h.candidate.full_name, h.reason) for h in hits] == [("a/r02", "理由三"), ("a/r00", "理由一")]  # LLM 顺序保持
    assert ai.select_calls and ai.select_calls[0][0]["id"] == 1  # 编号从 1 起
    conn.close()


def test_select_empty_results_not_fallback(tmp_path):
    """LLM 认为无匹配（空 results 数组）：空列表直出，不算精选失败（不触发粗排兜底）。"""
    conn = _open_db(tmp_path)
    _seed_select(conn, 3)
    base = {"keywords": ["crawler"], "languages": [], "topics": []}
    ai = FakeSearchAI(intent=base, select={"results": []})
    hits = asyncio.run(_select_with_ai(ai, _build_intent("crawler", base), recall_candidates(conn, _build_intent("crawler", base))))
    assert hits == []
    conn.close()


def test_select_parse_failure_falls_back_to_rank(tmp_path, caplog):
    """精选解析失败（None）：退化按粗排顺序直出（至多 20 条、无理由），记 WARNING。"""
    conn = _open_db(tmp_path)
    _seed_select(conn, 12)
    base = {"keywords": ["crawler"], "languages": [], "topics": []}
    ai = FakeSearchAI(intent=base, select=None)
    intent = _build_intent("crawler", base)
    with caplog.at_level("WARNING", logger="app.search"):
        hits = asyncio.run(_select_with_ai(ai, intent, recall_candidates(conn, intent)))
    assert len(hits) == 12  # 候选不足 20 按实际返回，不凑数
    assert [h.candidate.full_name for h in hits] == [f"a/r{i:02d}" for i in range(12)]  # 粗排顺序
    assert all(h.reason is None for h in hits)
    assert any("退化粗排直出" in r.message for r in caplog.records)
    conn.close()


def test_select_fallback_truncates_to_result_limit(tmp_path):
    """候选 >20 时退化直出截断到 RESULT_LIMIT（k3 评审 F3-2：10→20 后截断路径失去覆盖，补位）。"""
    conn = _open_db(tmp_path)
    _seed_select(conn, 25)
    base = {"keywords": ["crawler"], "languages": [], "topics": []}
    ai = FakeSearchAI(intent=base, select=None)
    intent = _build_intent("crawler", base)
    hits = asyncio.run(_select_with_ai(ai, intent, recall_candidates(conn, intent)))
    assert len(hits) == RESULT_LIMIT
    assert [h.candidate.full_name for h in hits] == [f"a/r{i:02d}" for i in range(RESULT_LIMIT)]
    conn.close()


def test_select_out_of_range_and_bad_reason_fall_back(tmp_path):
    """超范围编号 / 理由非字符串 / 重复编号：整体退化粗排直出（spec：非法返回/超范围引用 → 退化，防幻觉）。"""
    conn = _open_db(tmp_path)
    _seed_select(conn, 3)
    base = {"keywords": ["crawler"], "languages": [], "topics": []}
    bad_selects = (
        {"results": [{"id": 99, "reason": "越界"}]},
        {"results": [{"id": 1, "reason": 123}]},
        {"results": [{"id": "1", "reason": "字符串编号"}]},
        {"results": [{"id": 1, "reason": "重复"}, {"id": 1, "reason": "再选"}]},
        {"results": [{"id": 1}]},  # 缺 reason
        {"results": "not-a-list"},
        {"results": [42]},  # 元素非对象
    )
    for bad in bad_selects:
        ai = FakeSearchAI(intent=base, select=bad)
        intent = _build_intent("crawler", base)
        hits = asyncio.run(_select_with_ai(ai, intent, recall_candidates(conn, intent)))
        assert [h.candidate.full_name for h in hits] == [f"a/r{i:02d}" for i in range(3)]  # 粗排直出
        assert all(h.reason is None for h in hits)
    conn.close()


def test_select_call_failure_falls_back_auth_raises(tmp_path):
    """精选调用失败（DeepSeekError）：同退化口径粗排直出；AuthError 直通（调用方映射不可用）。"""
    conn = _open_db(tmp_path)
    _seed_select(conn, 3)
    base = {"keywords": ["crawler"], "languages": [], "topics": []}
    intent = _build_intent("crawler", base)
    ai_err = FakeSearchAI(intent=base, select_error=DeepSeekError("模拟超时"))
    hits = asyncio.run(_select_with_ai(ai_err, intent, recall_candidates(conn, intent)))
    assert [h.candidate.full_name for h in hits] == [f"a/r{i:02d}" for i in range(3)]
    assert all(h.reason is None for h in hits)
    ai_auth = FakeSearchAI(intent=base, select_error=DeepSeekAuthError("key 无效"))
    with pytest.raises(DeepSeekAuthError):
        asyncio.run(_select_with_ai(ai_auth, intent, recall_candidates(conn, intent)))
    conn.close()


# ---------- 4.4 池外补搜：补足到 20、池内在前池外在后、补搜失败降级、空结果如实说明 ----------


def test_external_supplements_to_limit(tmp_path):
    """池内 4 条 → 补搜补足：查询带关键词与语言限定（单语言不加括号）、sort=stars、已在池排除、池内在前池外在后。"""
    conn = _open_db(tmp_path)
    for i in range(4):
        _add_repo(conn, f"a/in{i}", description_en="crawler tool", stars=100)
    _add_repo(conn, "a/pooled", description_en="already in pool", stars=100)  # 已在池 → 补搜须排除
    conn.commit()
    base = {"keywords": ["crawler", "scraping"], "languages": ["Python"], "topics": []}
    intent = _build_intent("爬虫", base)
    items = [make_search_item("b/ext1", stars=5000), make_search_item("a/pooled", stars=9999), make_search_item("b/ext2", stars=4000)]
    gh = FakeSearchGitHub(pages=[items])
    hits = asyncio.run(search_external(conn, gh, intent, 2))
    assert [h.full_name for h in hits] == ["b/ext1", "b/ext2"]  # 已在池排除；补足所需条数即停
    assert gh.calls == [{"query": '(crawler OR scraping) language:"Python"', "sort": "stars", "per_page": 100, "page": 1}]
    assert hits[0].stars == 5000 and hits[0].created_year == 2024 and hits[0].description_en == "external desc"
    conn.close()


def test_external_second_page_when_first_insufficient(tmp_path):
    """一次 Search 调用不够 → 第二次调用（至多 1~2 次）；仍不足按实际返回不凑数。"""
    conn = _open_db(tmp_path)
    conn.commit()
    gh = FakeSearchGitHub(pages=[[make_search_item("b/one")], [make_search_item("b/two"), make_search_item("b/three")]])
    hits = asyncio.run(search_external(conn, gh, Intent("c", ["crawler"]), 3))
    assert [h.full_name for h in hits] == ["b/one", "b/two", "b/three"]
    assert [c["page"] for c in gh.calls] == [1, 2]
    conn.close()


def test_external_no_query_no_call(tmp_path):
    """关键词空且无语言限定：无查询可拼，直接空返回不打 GitHub。"""
    conn = _open_db(tmp_path)
    conn.commit()
    gh = FakeSearchGitHub()
    hits = asyncio.run(search_external(conn, gh, Intent("x", ["stars:>1000"], []), 3))  # 冒号 token 丢弃 → 无查询
    assert hits == [] and gh.calls == []
    conn.close()


def test_run_search_external_failure_and_auth_skip(tmp_path):
    """补搜失败/限速/token 缺失：跳过补搜，池内结果照常，external_failed 标注（页面如实显示）。"""
    conn = _open_db(tmp_path)
    _add_repo(conn, "a/in", description_en="crawler tool", stars=100)
    conn.commit()
    base = {"keywords": ["crawler"], "languages": [], "topics": []}
    gh_err = FakeSearchGitHub(error=GitHubError("模拟限速"))
    result = _run_search(conn, FakeSearchAI(intent=base, select={"results": [{"id": 1, "reason": "r"}]}), gh_err, "爬虫")
    assert result.external_failed is True
    assert [h.candidate.full_name for h in result.pool_hits] == ["a/in"]
    gh_auth = FakeSearchGitHub(error=GitHubAuthError("token 为空"))
    result2 = _run_search(conn, FakeSearchAI(intent=base, select={"results": []}), gh_auth, "爬虫")
    assert result2.external_failed is True and result2.pool_hits == []
    conn.close()


def test_run_search_unavailable_on_intent_call_failure(tmp_path):
    """意图理解调用失败（DeepSeekError）：搜索暂不可用（fail-loud），主链路不受影响。"""
    conn = _open_db(tmp_path)
    conn.commit()
    ai = FakeSearchAI(intent_error=DeepSeekError("模拟超时"))
    result = _run_search(conn, ai, FakeSearchGitHub(), "爬虫")
    assert result.unavailable is True
    assert result.intent is None and result.pool_hits == []
    conn.close()


def test_github_query_shape():
    """补搜查询拼接：单关键词单语言裸拼；多关键词/多语言加括号防 OR/AND 优先级吞语言限定；冒号 token 丢弃。"""
    assert _github_query(Intent("c", ["crawler"], ["Python"])) == 'crawler language:"Python"'
    assert _github_query(Intent("c", ["crawler", "scraping"], ["Python", "Go"])) == '(crawler OR scraping) (language:"Python" OR language:"Go")'
    assert _github_query(Intent("c", ["crawler", "stars:>1000"], [])) == "crawler"
    assert _github_query(Intent("c", [], [])) == ""


def test_github_query_operator_budget():
    """运算符预算（k3 评审 F2-1）：GitHub Search 单查询限 5 个 AND/OR/NOT——关键词截前 4、语言截前 2，
    最坏 3+1=4 个 OR；超限截断仅影响补搜覆盖面。"""
    q = _github_query(Intent("c", ["a", "b", "c", "d", "e", "f"], ["Python", "Go", "Rust"]))
    assert q == '(a OR b OR c OR d) (language:"Python" OR language:"Go")'
    assert q.count(" OR ") <= 4


# ---------- 4.5 关注闭环与页面（P8 /search + POST /search） ----------


@pytest.fixture()
def search_env(tmp_path, monkeypatch):
    """独立 tmp 库 + 关调度 + DEEPSEEK_API_KEY 默认配置（页面非不可用态）；假 client 由各用例注入。"""
    db = tmp_path / "search-web.db"
    init_db(db)
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    return db


def _web_client(db, ai, gh):
    app.dependency_overrides[_ai_client] = lambda: ai
    app.dependency_overrides[_github_client] = lambda: gh
    return TestClient(app)


def _seed_web(conn):
    """页面种子：池内 1 仓（Python 爬虫，带译文/标签/概要/创建年份/关注态）。"""
    rid = _add_repo(
        conn,
        "a/crawler",
        description_en="web scraping framework",
        topics=["ai"],
        stars=1200,
        github_created_at="2020-03-01T00:00:00Z",
    )
    conn.execute("UPDATE repos SET description_zh = '爬虫框架译文' WHERE id = ?", (rid,))
    conn.execute("INSERT INTO tags (repo_id, tag) VALUES (?, '选型观察')", (rid,))
    conn.execute(
        "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
        " VALUES (?, 'summary', 'all', '概要文本', NULL, '2026-W32')",
        (rid,),
    )
    conn.execute("INSERT INTO follows (repo_id, created_at) VALUES (?, '2026-08-01T00:00:00Z')", (rid,))
    conn.commit()


def test_search_page_ok_and_nav(search_env):
    """打开搜索页：200、顶栏 6 项含"搜索"入口（active）、输入框+提交按钮、无结果区。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    with _web_client(search_env, FakeSearchAI(), FakeSearchGitHub()) as client:
        resp = client.get("/search")
        assert resp.status_code == 200
        assert 'href="/search"' in resp.text and ">搜索</a>" in resp.text
        assert 'id="search-input"' in resp.text and 'id="search-btn"' in resp.text
        assert 'name="q"' in resp.text and 'action="/search"' in resp.text
        assert "搜索结果" not in resp.text  # 未搜索无结果区
        assert "搜索暂不可用" not in resp.text
    app.dependency_overrides.clear()


def test_search_page_unavailable_without_key(tmp_path, monkeypatch):
    """AI key 未配置：页面明确"搜索暂不可用（AI 未配置或已禁用）"＋表单禁用；榜单页不受影响。"""
    db = tmp_path / "no-key.db"
    init_db(db)
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    with TestClient(app) as client:
        resp = client.get("/search")
        assert resp.status_code == 200
        assert "搜索暂不可用（AI 未配置或已禁用）" in resp.text
        assert 'id="search-btn" disabled' in resp.text  # 按钮禁用
        assert 'required disabled' in resp.text  # 输入框禁用（disabled 在标签尾，不与 id 相邻）
        assert client.get("/").status_code == 200  # 主链路不受影响


def test_search_post_full_chain(search_env):
    """主路径：意图透明行 + 池内行（推荐理由/译文/标签/概要/年份）+ 池外行（徽标/原始描述/无 acts 操作/关注星）
    + 排序池内在前池外在后 + 计数标题。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    ai = FakeSearchAI(
        intent={"keywords": ["crawler", "scraping"], "languages": ["Python"], "topics": []},
        select={"results": [{"id": 1, "reason": "最成熟的 Python 爬虫框架。"}]},
    )
    gh = FakeSearchGitHub(pages=[[make_search_item("b/outside", description="external crawler", stars=8000)]])
    with _web_client(search_env, ai, gh) as client:
        resp = client.post("/search", data={"q": "我要做爬虫"})
        assert resp.status_code == 200
        text = resp.text
        # 意图透明行（§17.1）
        assert "理解为：" in text and "crawler" in text and "scraping" in text
        assert "语言：" in text and "Python" in text
        # 结果标题计数
        assert "2 条（池内 1 · 池外 1）" in text
        # 池内在前、池外在后
        assert text.index("a/crawler") < text.index("b/outside")
        # 池内行：推荐理由块（LLM 精选）+ 译文 + 标签 chip + 概要 + 创建年份（T-033 红线：必须传 created_year）
        assert "最成熟的 Python 爬虫框架。" in text and "爬虫框架译文" in text and "选型观察" in text
        assert "概要文本" in text and "· 2020" in text
        # 池外行：池外徽标 + GitHub 原始描述 + 关注星；acts 区只留 GitHub 链接（无打标/翻译按钮）
        assert ">池外</span>" in text and "external crawler" in text
        # 用 title 属性定位实际渲染行，避开隐藏字段中 prev_results JSON 也含 "b/outside"
        outside_section = text.split('title="b/outside"')[1]
        assert "tag-add" not in outside_section and "translate-btn" not in outside_section
        # 关注星：池内行（已关注 on）+ 池外行（关注入口）都有
        assert 'data-repo="a/crawler"' in text and 'data-repo="b/outside"' in text
        assert 'class="star on" data-repo="a/crawler"' in text
    app.dependency_overrides.clear()


def test_search_post_pool_insufficient_triggers_external(search_env):
    """池内不足 20 条触发补搜：池内 0 条 + 池外补足 → 池外行排后。"""
    conn = get_conn(search_env)
    conn.close()
    ai = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []},
        select={"results": []},  # LLM 无匹配：池内 0 条（不凑数）
    )
    gh = FakeSearchGitHub(pages=[[make_search_item("b/only")]])
    with _web_client(search_env, ai, gh) as client:
        text = client.post("/search", data={"q": "爬虫"}).text
        assert "1 条（池内 0 · 池外 1）" in text
        assert "b/only" in text
    app.dependency_overrides.clear()


def test_search_post_degraded_select_no_reason(search_env):
    """精选失败退化：池内行无推荐理由块（如实），行本身照常展示。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    ai = FakeSearchAI(intent={"keywords": ["crawler"], "languages": [], "topics": []}, select=None)
    with _web_client(search_env, ai, FakeSearchGitHub(pages=[[]])) as client:
        text = client.post("/search", data={"q": "爬虫"}).text
        assert "a/crawler" in text
        assert '<div class="reason">' not in text  # 无推荐理由块
        assert "理解为：" in text  # 意图透明照常
    app.dependency_overrides.clear()


def test_search_post_external_failed_notice(search_env):
    """补搜失败：池内结果照常 + 补搜失败标注（不报错页）。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    ai = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []},
        select={"results": [{"id": 1, "reason": "r"}]},
    )
    gh = FakeSearchGitHub(error=GitHubError("模拟限速"))
    with _web_client(search_env, ai, gh) as client:
        text = client.post("/search", data={"q": "爬虫"}).text
        assert "a/crawler" in text
        assert "GitHub 实时补搜失败" in text
    app.dependency_overrides.clear()


def test_search_post_all_empty_honest_notice(search_env):
    """池内+池外全空：如实空结果说明（带已理解关键词），无编造条目。"""
    conn = get_conn(search_env)
    conn.close()
    ai = FakeSearchAI(intent={"keywords": ["ghost"], "languages": [], "topics": []}, select={"results": []})
    gh = FakeSearchGitHub(pages=[[]])
    with _web_client(search_env, ai, gh) as client:
        text = client.post("/search", data={"q": "ghost"}).text
        assert "没有找到匹配「ghost」的项目，换个说法试试？" in text
        assert 'class="row' not in text
    app.dependency_overrides.clear()


def _extract_hidden(html: str, name: str) -> str:
    """从 SSR HTML 提取指定隐藏字段的 value（HTML 实体已转义，需 unescape）。"""
    import html as _html

    m = re.search(rf'<input[^>]*name="{name}"[^>]*value="([^"]*)"', html)
    assert m is not None, f"隐藏字段 {name} 未找到"
    return _html.unescape(m.group(1))


def test_search_post_follow_up_empty_keeps_previous_results(search_env):
    """追问搜空保留上一轮：页面显示新旧意图对比 + 上一轮结果区，且不发生额外 LLM 调用。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    # 首搜：池内 1 条 + 池外 0 条
    first_ai = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []},
        select={"results": [{"id": 1, "reason": "首搜理由"}]},
    )
    with _web_client(search_env, first_ai, FakeSearchGitHub(pages=[[]])) as client:
        first = client.post("/search", data={"q": "爬虫"}).text
        assert "a/crawler" in first
        prev_intent_value = _extract_hidden(first, "prev_intent")
        prev_results_value = _extract_hidden(first, "prev_results")

    # 追问：合并后新意图在池内/池外均无结果
    follow_ai = FakeSearchAI(
        intent={"keywords": ["ghost-term"], "languages": [], "topics": []},
        select={"results": []},
    )
    with _web_client(search_env, follow_ai, FakeSearchGitHub(pages=[[]])) as client:
        text = client.post("/search", data={"q": "只要 ghost 的", "prev_intent": prev_intent_value, "prev_results": prev_results_value}).text
        # 空结果说明
        assert "没有找到匹配" in text
        # 新旧意图对比 + 上一轮结果区
        assert "意图变化" in text
        assert "上一轮结果" in text
        assert "a/crawler" in text
        # 不额外调 LLM：追问只调 1 次 understand_intent（合并），select 因池空未调
        assert len(follow_ai.intent_calls) == 1
        assert follow_ai.select_calls == []
    app.dependency_overrides.clear()


def test_search_post_follow_up_empty_invalid_prev_results(search_env):
    """追问搜空但 prev_results 非法：只显示空结果说明，不渲染上一轮结果区。"""
    conn = get_conn(search_env)
    conn.close()
    ai = FakeSearchAI(intent={"keywords": ["ghost"], "languages": [], "topics": []}, select={"results": []})
    with _web_client(search_env, ai, FakeSearchGitHub(pages=[[]])) as client:
        text = client.post(
            "/search",
            data={"q": "更窄的", "prev_intent": '{"keywords":["crawler"],"languages":[],"topics":[]}', "prev_results": "not-json"},
        ).text
        assert "没有找到匹配" in text
        assert "上一轮结果" not in text
        assert "意图变化" not in text
        assert 'class="row' not in text
    app.dependency_overrides.clear()


def test_search_post_prev_intent_invalid_treated_as_first_search(search_env):
    """prev_intent 缺失或非法按首搜处理：本轮输入单独走意图理解，不进入合并链路。"""
    conn = get_conn(search_env)
    _add_repo(conn, "a/crawler", description_en="web crawler framework", language="Python", stars=1200)
    conn.commit()
    ai = FakeSearchAI(intent={"keywords": ["crawler"], "languages": [], "topics": []}, select={"results": [{"id": 1, "reason": "r"}]})
    with _web_client(search_env, ai, FakeSearchGitHub(pages=[[]])) as client:
        # prev_intent 非法 JSON
        text = client.post("/search", data={"q": "爬虫", "prev_intent": "bad-json"}).text
        assert "a/crawler" in text
        # 校验 LLM 调用未携带 prev_intent（None = 首搜）
        assert ai.intent_calls == [("爬虫", None)]
    app.dependency_overrides.clear()


def test_search_post_hidden_fields_carried(search_env):
    """结果页表单渲染 prev_intent/prev_results 隐藏字段，供下一问回传。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    ai = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []},
        select={"results": [{"id": 1, "reason": "r"}]},
    )
    with _web_client(search_env, ai, FakeSearchGitHub(pages=[[]])) as client:
        text = client.post("/search", data={"q": "爬虫"}).text
        assert 'name="prev_intent"' in text
        assert 'name="prev_results"' in text
        # prev_results JSON 中含当前结果行的 full_name
        assert "a/crawler" in _extract_hidden(text, "prev_results")
    app.dependency_overrides.clear()


def test_search_post_unavailable_on_auth(search_env):
    """执行期 AI 账户类错误：页面"搜索暂不可用"（fail-loud），主链路不受影响。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    ai = FakeSearchAI(intent_error=DeepSeekAuthError("DeepSeek 账户类错误（HTTP 401）"))
    with _web_client(search_env, ai, FakeSearchGitHub()) as client:
        text = client.post("/search", data={"q": "爬虫"}).text
        assert "搜索暂不可用" in text
        assert client.get("/total").status_code == 200  # 主链路不受影响
    app.dependency_overrides.clear()


def test_search_post_bad_query_400(search_env):
    """空搜索词 / 超长搜索词：400（fail-loud，不带着垃圾输入打 AI）。"""
    conn = get_conn(search_env)
    conn.close()
    ai = FakeSearchAI(intent={"keywords": ["k"], "languages": [], "topics": []})
    with _web_client(search_env, ai, FakeSearchGitHub()) as client:
        assert client.post("/search", data={"q": "   "}).status_code == 400
        assert client.post("/search", data={"q": "x" * 201}).status_code == 400
        assert ai.intent_calls == []  # 校验不过不打 AI
    app.dependency_overrides.clear()


def test_row_template_external_branches():
    """_row.html 池外行分支（spec 场景：已在池/已关注不显示关注入口；池外行 acts 只留 GitHub 链接）——
    直接渲染共享行模板断言；既有页面（不传 external）行为不变。"""
    env = templates.env

    def render(**extra):
        row = {
            "rank": 1,
            "full_name": "b/x",
            "language": None,
            "lang_color": "#8b98a9",
            "dead": False,
            "external": False,
            "up_na_text": None,
            "delta_text": None,
            "delta_neg": False,
            "stars_text": "5.0k",
            "followed": False,
            "description_en": "desc",
            "description_zh": None,
            "reason": None,
            "summary": None,
            "tags": [],
            "endpoint_note": None,
            "window_note": None,
            "created_year": None,
            "reason_dim": "total",
            "reason_period_label": "all",
            "reason_label": "推荐理由",
            "show_recommend": False,
        }
        row.update(extra)
        return env.get_template("_row.html").render(anchor="s", idx=0, r=row)

    # 池外未关注：显示关注入口 + 池外徽标 + 无打标/翻译按钮，gh 链接保留
    html = render(external=True, up_na_text="——", followed=False)
    assert ">池外</span>" in html
    assert 'class="star' in html
    assert "tag-add" not in html and "translate-btn" not in html
    assert "在 GitHub 打开" in html
    # 已在池/已关注的池外行：不显示关注入口（spec 场景）
    html_followed = render(external=True, followed=True)
    assert 'class="star' not in html_followed
    # 既有页面行（不传 external）：行为不变（星标/打标/翻译都在）
    html_pool = render()
    assert 'class="star' in html_pool and "tag-add" in html_pool and "translate-btn" in html_pool
    assert ">池外</span>" not in html_pool


def test_search_external_follow_closes_loop(search_env):
    """池外行关注闭环：点击行尾关注星走既有 /api/follows 三态接线——未入池仓动态入池
    （source='follow' + 基线快照），搜索成为池子的有机扩充入口（spec 决策 5）。"""
    conn = get_conn(search_env)
    conn.close()
    ai = FakeSearchAI(intent={"keywords": ["crawler"], "languages": [], "topics": []}, select={"results": []})
    gh = FakeSearchGitHub(
        pages=[[make_search_item("b/outside", stars=9000, created_at="2023-05-01T00:00:00.123Z")]],
        fetch_items={"b/outside": make_search_item("b/outside", stars=9000, created_at="2023-05-01T00:00:00.123Z")},
    )
    with _web_client(search_env, ai, gh) as client:
        text = client.post("/search", data={"q": "爬虫"}).text
        assert 'data-repo="b/outside"' in text  # 池外行带关注星（关注入口）
        resp = client.post("/api/follows", json={"full_name": "b/outside"})
        assert resp.status_code == 200 and resp.json()["state"] == "joined"
        row = get_conn(search_env).execute(
            "SELECT source, github_created_at FROM repos WHERE full_name = 'b/outside'"
        ).fetchone()
        assert row is not None and row["source"] == "follow"  # 动态入池（决策 9 链路）
        assert row["github_created_at"] == "2023-05-01T00:00:00Z"  # 毫秒归一化入库（T-033 同口径）
        snap = get_conn(search_env).execute(
            "SELECT stars FROM star_snapshots WHERE repo_id = (SELECT id FROM repos WHERE full_name = 'b/outside')"
        ).fetchone()
        assert snap["stars"] == 9000  # 基线快照
    app.dependency_overrides.clear()


# ---------- T-036 追加：filters 白名单、unsupported 仅展示、召回/补搜过滤、追问合并 ----------


def test_intent_build_filters_and_unsupported():
    """filters 白名单字段与 unsupported 字符串数组正常进入 Intent。"""
    intent = _build_intent(
        "x",
        {
            "keywords": ["k"],
            "languages": [],
            "topics": [],
            "filters": {"created_within_days": 365, "min_stars": 5000},
            "unsupported": ["最近一周有提交"],
        },
    )
    assert intent.filters == {"created_within_days": 365, "min_stars": 5000}
    assert intent.unsupported == ["最近一周有提交"]
    assert "创建：近 1 年内" in intent.display_text
    assert "星数 ≥5000" in intent.display_text
    assert "暂不支持：最近一周有提交" in intent.display_text


def test_intent_build_invalid_filters_dropped_with_warning(caplog):
    """白名单外字段/类型非法/非正整数 → 丢弃该字段记 WARNING，其余合法字段仍生效。"""
    with caplog.at_level("WARNING", logger="app.search"):
        intent = _build_intent(
            "x",
            {
                "keywords": ["k"],
                "languages": [],
                "topics": [],
                "filters": {
                    "created_within_days": 365,
                    "forks_min": 100,
                    "min_stars": "5000",
                    "bad_bool": True,
                    "negative": -1,
                },
                "unsupported": ["a", 123, "", "b"],
            },
        )
    assert intent.filters == {"created_within_days": 365}
    assert intent.unsupported == ["a", "b"]
    assert any("白名单外字段丢弃" in r.message for r in caplog.records)
    assert any("类型非法丢弃" in r.message for r in caplog.records)


def test_intent_display_text_days_not_multiple_of_365():
    """created_within_days 不能整除 365 时显示'近 N 天内'。"""
    intent = _build_intent(
        "x",
        {"keywords": ["k"], "languages": [], "topics": [], "filters": {"created_within_days": 180}},
    )
    assert "创建：近 180 天内" in intent.display_text
    assert "年" not in intent.display_text


def _iso_now() -> str:
    """当前 UTC 的 ISO 定长字符串（与 schema 同口径）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_recall_filters_created_within_days_and_min_stars(tmp_path):
    """filters 叠加过滤：created_within_days 排除 NULL 与超期；min_stars 排除星数不足。"""
    fresh = _iso_now()
    old = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _open_db(tmp_path)
    # 命中关键词，但创建时间 NULL
    _add_repo(conn, "a/no-created", description_en="crawler tool", stars=9000, github_created_at=None)
    # 命中，创建时间太近（在 N 天内）
    _add_repo(conn, "a/fresh", description_en="crawler tool", stars=9000, github_created_at=fresh)
    # 命中，创建时间太远
    _add_repo(conn, "a/old", description_en="crawler tool", stars=9000, github_created_at=old)
    # 命中，星数不足
    _add_repo(conn, "a/low-stars", description_en="crawler tool", stars=100, github_created_at=fresh)
    conn.commit()
    intent = Intent(
        "crawler",
        ["crawler"],
        filters={"created_within_days": 30, "min_stars": 1000},
    )
    cands = recall_candidates(conn, intent)
    assert [c.full_name for c in cands] == ["a/fresh"]
    conn.close()


def test_recall_filter_created_null_excluded(tmp_path):
    """created_within_days 过滤：github_created_at 为 NULL 的仓不入选。"""
    fresh = _iso_now()
    conn = _open_db(tmp_path)
    _add_repo(conn, "a/null-created", description_en="crawler tool", stars=9000, github_created_at=None)
    _add_repo(conn, "a/fresh", description_en="crawler tool", stars=9000, github_created_at=fresh)
    conn.commit()
    cands = recall_candidates(conn, Intent("crawler", ["crawler"], filters={"created_within_days": 30}))
    assert [c.full_name for c in cands] == ["a/fresh"]
    conn.close()


def test_github_query_with_filters():
    """_github_query 追加 filters 限定词：created:>YYYY-MM-DD、stars:>=N；限定词不占运算符预算。"""
    import re

    # 4 关键词（3 个 OR）＋2 语言（1 个 OR）恰满 4 个运算符预算，filters 限定词若计入必然超 4
    intent = Intent(
        "crawler",
        ["crawler", "scraping", "spider", "scrape"],
        ["Python", "Go"],
        filters={"created_within_days": 365, "min_stars": 5000},
    )
    q = _github_query(intent)
    assert re.search(r"created:>\d{4}-\d{2}-\d{2}", q)
    assert "stars:>=5000" in q
    assert "(crawler OR scraping OR spider OR scrape)" in q
    assert '(language:"Python" OR language:"Go")' in q
    # 限定词不计入 OR 预算（恰满 4，多一个即破）
    assert q.count(" OR ") == 4


def test_run_search_follow_up_merge_filters_override(tmp_path):
    """追问合并：LLM 返回的合并意图中 filters 同名覆盖、未提及保留；服务端把旧 filters/unsupported 传给 LLM。"""
    conn = _open_db(tmp_path)
    _add_repo(conn, "a/crawler", description_en="web crawler framework", language="Python", stars=2000)
    conn.commit()
    old = Intent(
        "我要做爬虫",
        ["crawler", "scraping"],
        ["Python"],
        filters={"created_within_days": 365, "min_stars": 1000},
        unsupported=["最近一周有提交"],
    )
    # LLM 合并结果：created_within_days 被覆盖，min_stars 保留，unsupported 清空
    merged = {
        "keywords": ["crawler", "scraping"],
        "languages": ["Python"],
        "topics": [],
        "filters": {"created_within_days": 1095, "min_stars": 1000},
        "unsupported": [],
    }
    ai = FakeSearchAI(intent=merged, select={"results": [{"id": 1, "reason": "理由"}]})
    result = _run_search(conn, ai, FakeSearchGitHub(), "放宽到 3 年内", prev_intent=old)
    assert result.intent.filters == {"created_within_days": 1095, "min_stars": 1000}
    assert result.intent.unsupported == []
    assert ai.intent_calls[0][1]["filters"] == {"created_within_days": 365, "min_stars": 1000}
    assert ai.intent_calls[0][1]["unsupported"] == ["最近一周有提交"]
    conn.close()


def test_search_post_filters_and_unsupported_rendered(search_env):
    """意图透明行渲染 filters 生效段与 unsupported 段。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    ai = FakeSearchAI(
        intent={
            "keywords": ["crawler"],
            "languages": [],
            "topics": [],
            "filters": {"created_within_days": 365, "min_stars": 5000},
            "unsupported": ["最近一周有提交"],
        },
        select={"results": [{"id": 1, "reason": "r"}]},
    )
    with _web_client(search_env, ai, FakeSearchGitHub(pages=[[]])) as client:
        text = client.post("/search", data={"q": "爬虫"}).text
        assert "创建：近 1 年内" in text
        assert "星数 ≥5000" in text
        assert "暂不支持：最近一周有提交" in text
    app.dependency_overrides.clear()


def test_search_post_prev_intent_filters_parsed(search_env):
    """追问回传的 prev_intent 含 filters/unsupported 时正确解析并进入合并链路。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    first_ai = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []},
        select={"results": [{"id": 1, "reason": "r"}]},
    )
    with _web_client(search_env, first_ai, FakeSearchGitHub(pages=[[]])) as client:
        first = client.post("/search", data={"q": "爬虫"}).text
        prev_intent_value = _extract_hidden(first, "prev_intent")

    follow_ai = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []},
        select={"results": [{"id": 1, "reason": "r"}]},
    )
    with _web_client(search_env, follow_ai, FakeSearchGitHub(pages=[[]])) as client:
        client.post("/search", data={"q": "更窄的", "prev_intent": prev_intent_value}).text
        # 合并调用携带的 prev_intent 含 filters/unsupported（即使为空对象/数组）
        assert follow_ai.intent_calls[0][1]["filters"] == {}
        assert follow_ai.intent_calls[0][1]["unsupported"] == []


# ---------- T-041 搜索流式加载态：run_search 插桩时序（任务 2.4）与 /search/stream 端点（2.5/2.6） ----------


def _stage_events(events: list[dict]) -> list[tuple[str, str]]:
    """从回调事件序列筛出阶段事件（stage, state）——时序断言用。"""
    return [(e["stage"], e["state"]) for e in events if e.get("type") == "stage"]


def _seed_pool_one(conn):
    """链路级用例种子：池内 1 仓（Python 爬虫，带翻译/标签/概要/关注态），供 page 层之外的 run_search 用例复用。"""
    rid = _add_repo(conn, "a/crawler", description_en="web scraping framework", topics=["ai"], stars=1200)
    conn.execute("UPDATE repos SET description_zh = '爬虫框架译文' WHERE id = ?", (rid,))
    conn.execute("INSERT INTO tags (repo_id, tag) VALUES (?, '选型观察')", (rid,))
    conn.execute(
        "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
        " VALUES (?, 'summary', 'all', '概要文本', NULL, '2026-W32')",
        (rid,),
    )
    conn.execute("INSERT INTO follows (repo_id, created_at) VALUES (?, '2026-08-01T00:00:00Z')", (rid,))
    conn.commit()


def test_run_search_progress_events_execute_and_skip(tmp_path):
    """阶段事件时序：执行补搜＝external start/done；池内已足 20 条＝external skip（如实标注不伪造成进行中）。"""
    conn = _open_db(tmp_path)
    _seed_pool_one(conn)
    ai = FakeSearchAI(intent={"keywords": ["crawler"], "languages": [], "topics": []}, select={"results": [{"id": 1, "reason": "r"}]})
    events: list[dict] = []
    result = asyncio.run(
        run_search(
            conn,
            ai,
            FakeSearchGitHub(pages=[[make_search_item("b/outside")]]),
            "爬虫",
            progress_cb=events.append,
        )
    )
    assert [e["type"] for e in events] == ["stage"] * 8  # 四阶段各起止（补搜执行）
    assert _stage_events(events) == [
        ("intent", "start"),
        ("intent", "done"),
        ("recall", "start"),
        ("recall", "done"),
        ("select", "start"),
        ("select", "done"),
        ("external", "start"),
        ("external", "done"),
    ]
    assert result.pool_hits and result.external_hits  # 结果照常

    # 池内已足 20 条：补搜跳过态（不出现 external start）
    conn2 = _open_db(tmp_path / "two")
    for i in range(20):
        _add_repo(conn2, f"a/r{i:02d}", description_en="crawler framework")
    conn2.commit()
    ai20 = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []},
        select={"results": [{"id": i, "reason": "r"} for i in range(1, 21)]},
    )
    events20: list[dict] = []
    gh20 = FakeSearchGitHub(pages=[[make_search_item("b/outside")]])
    result20 = asyncio.run(run_search(conn2, ai20, gh20, "爬虫", progress_cb=events20.append))
    assert len(result20.pool_hits) == 20
    assert gh20.calls == []  # 补搜未触发（真实跳过）
    assert ("external", "skip") in _stage_events(events20)
    assert ("external", "start") not in _stage_events(events20)
    conn.close()
    conn2.close()


def test_run_search_progress_events_degraded_paths(tmp_path):
    """降级路径事件如实：精选调用失败（粗排直出）仍 select done；补搜失败 external failed；
    意图理解失败 intent failed 且不产生后续阶段事件。"""
    conn = _open_db(tmp_path)
    _seed_pool_one(conn)
    ai_fallback = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []}, select_error=DeepSeekError("boom")
    )
    events: list[dict] = []
    result = asyncio.run(run_search(conn, ai_fallback, FakeSearchGitHub(pages=[[]]), "爬虫", progress_cb=events.append))
    assert result.pool_hits  # 粗排直出
    assert result.pool_hits[0].reason is None
    assert ("select", "done") in _stage_events(events)

    events2: list[dict] = []
    ai_ok = FakeSearchAI(intent={"keywords": ["crawler"], "languages": [], "topics": []}, select={"results": [{"id": 1, "reason": "r"}]})
    result2 = asyncio.run(
        run_search(conn, ai_ok, FakeSearchGitHub(error=GitHubError("限速")), "爬虫", progress_cb=events2.append)
    )
    assert result2.external_failed is True
    assert ("external", "failed") in _stage_events(events2)

    events3: list[dict] = []
    ai_down = FakeSearchAI(intent_error=DeepSeekError("调用失败"))
    result3 = asyncio.run(run_search(conn, ai_down, FakeSearchGitHub(pages=[[]]), "爬虫", progress_cb=events3.append))
    assert result3.unavailable is True
    assert _stage_events(events3) == [("intent", "start"), ("intent", "failed")]  # 不产生后续阶段事件

    events4: list[dict] = []
    ai_auth = FakeSearchAI(intent_error=DeepSeekAuthError("key 无效"))
    result4 = asyncio.run(run_search(conn, ai_auth, FakeSearchGitHub(pages=[[]]), "爬虫", progress_cb=events4.append))
    assert result4.unavailable is True
    assert ("intent", "failed") in _stage_events(events4)
    conn.close()


def test_run_search_optional_callbacks_default_none_no_events(tmp_path):
    """缺省 None 零回归（任务 2.4/决策 6）：不传 progress_cb/thinking_cb 时无事件产生、
    thinking_cb 亦不进两段调用（假 client 未收到 thinking_cb，行为与插桩前一致）。"""
    conn = _open_db(tmp_path)
    _seed_pool_one(conn)
    ai = FakeSearchAI(intent={"keywords": ["crawler"], "languages": [], "topics": []}, select={"results": [{"id": 1, "reason": "r"}]})
    result = asyncio.run(run_search(conn, ai, FakeSearchGitHub(pages=[[]]), "爬虫"))
    assert result.pool_hits and result.pool_hits[0].reason == "r"
    assert ai.thinking_calls == []  # 缺省不触发流式路径


def test_run_search_thinking_cb_wired_to_both_llm_stages(tmp_path):
    """thinking_cb 透传两段调用：意图理解段标 intent、精选段标 select，增量原样上抛。"""
    conn = _open_db(tmp_path)
    _seed_pool_one(conn)
    ai = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []},
        select={"results": [{"id": 1, "reason": "r"}]},
        intent_thinking=["想关键词…"],
        select_thinking=["挑第 1 条"],
    )
    seen: list[tuple[str, str]] = []
    asyncio.run(
        run_search(
            conn,
            ai,
            FakeSearchGitHub(pages=[[]]),
            "爬虫",
            thinking_cb=lambda stage, delta: seen.append((stage, delta)),
        )
    )
    assert seen == [("intent", "想关键词…"), ("select", "挑第 1 条")]


def test_search_stream_ndjson_event_sequence(search_env):
    """流式端点（任务 2.5）：NDJSON 逐行可解析；stage/thinking/result 次序正确；
    result 事件 HTML 含意图透明行与结果行（_row.html 形态）、prev_intent/prev_results 合法 JSON。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    ai = FakeSearchAI(
        intent={"keywords": ["crawler", "scraping"], "languages": ["Python"], "topics": []},
        select={"results": [{"id": 1, "reason": "最成熟的 Python 爬虫框架。"}]},
        intent_thinking=["The user wants ", "a crawler."],
        select_thinking=["Candidate 1 fits best."],
    )
    gh = FakeSearchGitHub(pages=[[make_search_item("b/outside", description="external crawler", stars=8000)]])
    with _web_client(search_env, ai, gh) as client:
        resp = client.post("/search/stream", data={"q": "我要做爬虫"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    app.dependency_overrides.clear()
    types = [e["type"] for e in events]
    assert types[-1] == "result" and types.count("result") == 1
    assert _stage_events(events) == [
        ("intent", "start"),
        ("intent", "done"),
        ("recall", "start"),
        ("recall", "done"),
        ("select", "start"),
        ("select", "done"),
        ("external", "start"),
        ("external", "done"),
    ]
    thinking = [e for e in events if e["type"] == "thinking"]
    assert [(e["stage"], e["delta"]) for e in thinking] == [
        ("intent", "The user wants "),
        ("intent", "a crawler."),
        ("select", "Candidate 1 fits best."),
    ]
    assert types.index("thinking") < types.index("result")  # 思维链先于结果帧
    final = events[-1]
    assert "理解为：" in final["html"] and "crawler" in final["html"]
    assert 'title="a/crawler"' in final["html"] and 'class="row' in final["html"]
    assert "最成熟的 Python 爬虫框架。" in final["html"] and "b/outside" in final["html"]
    assert json.loads(final["prev_intent"])["keywords"] == ["crawler", "scraping"]
    assert [row["full_name"] for row in json.loads(final["prev_results"])] == ["a/crawler", "b/outside"]


def test_search_stream_no_thinking_events_when_model_lacks_reasoning(search_env):
    """无 reasoning 字段退化（任务 2.5/决策 4）：零 thinking 事件，阶段与结果照常（前端因此不渲染思考区）。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    ai = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []},
        select={"results": [{"id": 1, "reason": "r"}]},
    )
    with _web_client(search_env, ai, FakeSearchGitHub(pages=[[]])) as client:
        events = [json.loads(line) for line in client.post("/search/stream", data={"q": "爬虫"}).text.splitlines() if line.strip()]
    app.dependency_overrides.clear()
    assert [e for e in events if e["type"] == "thinking"] == []
    assert events[-1]["type"] == "result"


def test_search_stream_unavailable_renders_notice_fragment(search_env):
    """执行期 AI 不可用：result 帧仍下发（走同一 partial 的"搜索暂不可用"分支），与同步路径同形态。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    ai = FakeSearchAI(intent_error=DeepSeekAuthError("key 无效"))
    with _web_client(search_env, ai, FakeSearchGitHub(pages=[[]])) as client:
        events = [json.loads(line) for line in client.post("/search/stream", data={"q": "爬虫"}).text.splitlines() if line.strip()]
    app.dependency_overrides.clear()
    assert ("intent", "failed") in _stage_events(events)
    assert events[-1]["type"] == "result"
    assert "搜索暂不可用：key 无效" in events[-1]["html"]


def test_search_stream_follow_up_empty_keeps_previous_results(search_env):
    """追问搜空：流式 result 帧与同步路径同分支——保留上一轮结果区＋意图对比，prev_* 与展示行一致。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    first_ai = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []},
        select={"results": [{"id": 1, "reason": "r"}]},
    )
    with _web_client(search_env, first_ai, FakeSearchGitHub(pages=[[]])) as client:
        first = client.post("/search/stream", data={"q": "爬虫"}).text
    app.dependency_overrides.clear()
    first_result = json.loads([line for line in first.splitlines() if line.strip()][-1])
    # 追问：换成无命中查询（池内无、补搜空）→ 保留上一轮
    follow_ai = FakeSearchAI(intent={"keywords": ["ghost"], "languages": [], "topics": []}, select={"results": []})
    with _web_client(search_env, follow_ai, FakeSearchGitHub(pages=[[]])) as client:
        follow = client.post(
            "/search/stream",
            data={"q": "更窄的", "prev_intent": first_result["prev_intent"], "prev_results": first_result["prev_results"]},
        ).text
    app.dependency_overrides.clear()
    follow_result = json.loads([line for line in follow.splitlines() if line.strip()][-1])
    assert "上一轮结果" in follow_result["html"] and "意图变化" in follow_result["html"]
    assert "a/crawler" in follow_result["html"]
    # prev_results 与同步路径同源同口径（= 本轮展示结果行；搜空轮为空列表，上一轮区靠上一轮回传值渲染）
    assert follow_result["prev_results"] == "[]"


def _result_fragment(text: str) -> str:
    """从响应正文中取 #search-result 容器内容（同步页面用，做与流式 result 帧的片段比对）。"""
    m = re.search(r'<div id="search-result">(.*)</div>\s*</section>', text, re.S)
    assert m is not None, "同步页面未找到 #search-result 容器"
    return m.group(1).strip()


def test_search_stream_fragment_matches_sync_post(search_env):
    """两路径一致性（任务 2.6）：同一 stub 下流式 result 片段与同步 POST 结果区内容一致
    （同 partial 渲染，仅传输形态不同）；表单校验口径一致（空 q/超长 400，同文案）。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()

    def canned_ai():
        return FakeSearchAI(
            intent={"keywords": ["crawler"], "languages": [], "topics": []},
            select={"results": [{"id": 1, "reason": "r"}]},
        )

    gh_page = [make_search_item("b/outside", description="external crawler", stars=8000)]
    with _web_client(search_env, canned_ai(), FakeSearchGitHub(pages=[gh_page])) as client:
        sync_text = client.post("/search", data={"q": "爬虫"}).text
    app.dependency_overrides.clear()
    with _web_client(search_env, canned_ai(), FakeSearchGitHub(pages=[gh_page])) as client:
        stream_text = client.post("/search/stream", data={"q": "爬虫"}).text
    app.dependency_overrides.clear()
    stream_result = json.loads([line for line in stream_text.splitlines() if line.strip()][-1])
    assert stream_result["html"].strip() == _result_fragment(sync_text)

    with _web_client(search_env, canned_ai(), FakeSearchGitHub(pages=[[]])) as client:
        for payload in ({"q": ""}, {"q": "长" * 201}):
            sync_resp = client.post("/search", data=payload)
            stream_resp = client.post("/search/stream", data=payload)
            assert sync_resp.status_code == stream_resp.status_code == 400
            assert sync_resp.json()["detail"] == stream_resp.json()["detail"]
        form_text = client.get("/search").text
    app.dependency_overrides.clear()
    assert 'action="/search"' in form_text  # 表单原生提交目标仍是同步路径（无 JS 降级成立）
    assert 'id="search-result"' in form_text and 'id="search-progress"' in form_text  # 流式挂载点
    assert "/search/stream" in open("app/web/static/radar.js", encoding="utf-8").read()  # 前端接线（零构建，直接 grep）


def test_search_stream_prev_results_field_names_match_hidden_inputs(search_env):
    """无状态回传口径不变（任务 2.6）：流式 result 帧的两个 JSON 值与同步路径隐藏字段值同源同形态
    （可直接写回 prev_intent/prev_results 隐藏字段供下一问回传）。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()

    def canned_ai():
        return FakeSearchAI(
            intent={"keywords": ["crawler"], "languages": ["Python"], "topics": []},
            select={"results": [{"id": 1, "reason": "r"}]},
        )

    with _web_client(search_env, canned_ai(), FakeSearchGitHub(pages=[[]])) as client:
        sync_text = client.post("/search", data={"q": "爬虫"}).text
    app.dependency_overrides.clear()
    with _web_client(search_env, canned_ai(), FakeSearchGitHub(pages=[[]])) as client:
        stream_result = json.loads(
            [line for line in client.post("/search/stream", data={"q": "爬虫"}).text.splitlines() if line.strip()][-1]
        )
    app.dependency_overrides.clear()
    assert stream_result["prev_intent"] == _extract_hidden(sync_text, "prev_intent")
    assert stream_result["prev_results"] == _extract_hidden(sync_text, "prev_results")


# ---------- T-041 评审修复（agent-859）：前端契约锁（F1 / F2-2 / F2-3） ----------
# 项目无 JS 测试运行器（统一验证入口只跑 ruff＋pytest），前端行为由 A 级浏览器走查实证；
# 本组锁"跨文件契约"——正是三条 finding 的原形缺陷（写回静默丢失 / 类名与 CSS 不对齐 /
# result 帧覆盖如实终态），任一条被改回去都会在此红。


def _radar_js() -> str:
    import pathlib

    return (pathlib.Path(__file__).resolve().parents[1] / "app/web/static/radar.js").read_text(encoding="utf-8")


def _radar_css() -> str:
    import pathlib

    return (pathlib.Path(__file__).resolve().parents[1] / "app/web/static/radar.css").read_text(encoding="utf-8")


def test_search_page_has_no_prev_hidden_inputs_first_search(search_env):
    """F1 前置条件（bug 成因）：首搜页面（GET /search）模板条件渲染，两个追问隐藏字段不存在——
    流式路径不整页重载，故 JS 必须动态创建后才能写回；同步路径靠整页 SSR 重渲染不受影响。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    with _web_client(search_env, FakeSearchAI(), FakeSearchGitHub()) as client:
        page = client.get("/search").text
    app.dependency_overrides.clear()
    assert 'id="search-result"' in page and 'id="search-progress"' in page
    assert 'name="prev_intent"' not in page and 'name="prev_results"' not in page  # 首搜无隐藏字段


def test_search_stream_result_carries_prev_values_for_writeback(search_env):
    """F1 数据面：流式 result 帧恒携带两个字符串值（含 unavailable 退化帧），前端写回才有料可写；
    写回目标字段缺失时由 JS 动态创建（JS 侧契约见 test_radar_js_*）。"""
    conn = get_conn(search_env)
    _seed_web(conn)
    conn.close()
    ai = FakeSearchAI(
        intent={"keywords": ["crawler"], "languages": [], "topics": []},
        select={"results": [{"id": 1, "reason": "r"}]},
    )
    with _web_client(search_env, ai, FakeSearchGitHub(pages=[[]])) as client:
        events = [json.loads(line) for line in client.post("/search/stream", data={"q": "爬虫"}).text.splitlines() if line.strip()]
    app.dependency_overrides.clear()
    final = events[-1]
    assert isinstance(final["prev_intent"], str) and json.loads(final["prev_intent"])["keywords"] == ["crawler"]
    assert isinstance(final["prev_results"], str) and json.loads(final["prev_results"])[0]["full_name"] == "a/crawler"

    ai_down = FakeSearchAI(intent_error=DeepSeekAuthError("key 无效"))
    with _web_client(search_env, ai_down, FakeSearchGitHub(pages=[[]])) as client:
        events_down = [
            json.loads(line) for line in client.post("/search/stream", data={"q": "爬虫"}).text.splitlines() if line.strip()
        ]
    app.dependency_overrides.clear()
    assert isinstance(events_down[-1]["prev_intent"], str) and isinstance(events_down[-1]["prev_results"], str)


def test_radar_js_creates_missing_prev_hidden_inputs():
    """F1 修复锁：writeSearchHidden 在字段缺失时 createElement 创建 hidden 并经 appendChild 挂进表单，
    再赋值——否则首搜完成后的流式结果写回静默丢失，追问上下文断链（评审 F1）。"""
    js = _radar_js()
    m = re.search(r"function writeSearchHidden\(form, name, value\) \{(.*?)\n\}", js, re.S)
    assert m is not None, "writeSearchHidden 未找到（函数被改名/删除？）"
    body = m.group(1)
    assert 'createElement("input")' in body, "缺 createElement 创建逻辑（F1 回归）"
    assert 'input.type = "hidden"' in body and "input.name = name" in body, "动态创建的输入必须是同名 hidden"
    assert "form.appendChild(input)" in body, "未挂进表单则 FormData 取不到（F1 回归）"
    assert "input.value = value == null" in body, "赋值分支缺失"


def test_radar_js_step_state_class_mapping_aligned_with_css():
    """F2-2 修复锁：服务端阶段态 → CSS 类名映射在 JS 侧单点，且 JS 写入的每个类名 CSS 均有选择器
    （曾写 `sp-step start` 而 radar.css 只有 `.sp-step.active`，进行中呼吸点永不渲染）。"""
    js, css = _radar_js(), _radar_css()
    m = re.search(r"const SEARCH_STEP_CLASS = \{([^}]*)\}", js)
    assert m is not None, "步骤态→类名映射表未找到"
    mapping = dict(re.findall(r"(\w+):\s*\"(\w+)\"", m.group(1)))
    assert set(mapping) == {"start", "done", "skip", "failed"}, f"服务端四态未全覆盖：{sorted(mapping)}"
    assert mapping["start"] == "active", "进行中态必须映射到 CSS 既有 .sp-step.active"
    for cls in mapping.values():
        assert f".sp-step.{cls}" in css, f"CSS 缺 .sp-step.{cls} 选择器（JS 写但样式不存在）"
    assert '"sp-step" + (SEARCH_STEP_CLASS[state]' in js, "类名必须经映射表写入（不得回退直写服务端态名）"


def test_radar_js_result_frame_preserves_skip_and_failed_states():
    """F2-3 修复锁：result 帧只把"进行中未收尾"（dataset.state === 'start'）的步骤补标为已完成，
    skip（无需补搜）/ failed（失败跳过）保持如实终态——不得再无条件全量置 done。"""
    js = _radar_js()
    assert 'step.dataset.state === "start"' in js, "result 帧未按已记录态补标（F2-3 回归）"
    assert 'setSearchStep(progressBox, stage, "done")' in js, "补标动作缺失"
    assert 'for (const stage of Object.keys(SEARCH_STAGE_LABELS)) setSearchStep(progressBox, stage, "done");' not in js, (
        "无条件全量置 done 的旧写法仍在（会覆盖 skip/failed 如实态）"
    )
    assert 'step.dataset.state = state || ""' in js, "阶段态未记录到 dataset，result 帧无从判定"


def test_radar_js_stream_failure_falls_back_to_native_submit():
    """F3-5 修复锁：流失败收敛＝如实提示＋form.submit() 原生提交（整页导航销毁本页 JS 状态，
    "自动重跑仅一次"结构性成立）；原生提交抛错时才停留提示由用户手动重试。"""
    js = _radar_js()
    m = re.search(r"function failSearchStream\((.*?)\) \{(.*?)\n\}", js, re.S)
    assert m is not None, "failSearchStream 未找到"
    assert "form.submit()" in m.group(2), "未走原生提交（降级收敛路径缺失）"
    assert "onManualRetry" in m.group(2), "原生提交不可用时的停留提示分支缺失"
    assert "searchStreamRetried" not in js, "重试计数变量已废弃（可达性由整页导航保证），不得残留"


def test_radar_js_step_elapsed_timer():
    """T-041 走查打回修复锁（2026-10-13）：进行中步骤显示已用时秒数、每秒跳动——网关/模型首 token
    静默 20~60s 里呼吸点之外再给一个存活信号。计时器 id 挂 box 属性：box.textContent="" 只销毁
    DOM、停不掉 setInterval，故新 start 前先清旧的，且两个清箱入口清箱前必须停表（否则泄漏空转）。"""
    js, css = _radar_js(), _radar_css()

    def _fn(pattern, name):
        m = re.search(pattern, js, re.S)
        assert m is not None, f"{name} 未找到（函数被改名/删除？）"
        return m.group(1)

    render = _fn(r"function renderSearchProgress\(box\) \{(.*?)\n\}", "renderSearchProgress")
    assert 'time.className = "sp-time"' in render, "步骤条未创建 .sp-time 计时元素（存活信号缺失）"
    assert "label.appendChild(time)" in render, "计时元素未挂 label 内（直挂 .sp-step 会被纵排挤到下一行且高度跳动）"
    assert "time.textContent" not in render, "计时元素必须初始为空（由 :empty 收起，不预置文本）"
    assert 'time.setAttribute("aria-hidden", "true")' in render, "逐秒改文本须 aria-hidden（面板是 aria-live 区）"

    setter = _fn(r"function setSearchStep\(box, stage, state\) \{(.*?)\n\}", "setSearchStep")
    assert 'if (state === "start") startSearchStepTimer(box, step);' in setter, "进行中态未启动计时"
    assert "else clearSearchStepTimer(box);" in setter, "终态（done/skip/failed）未停表并清空"

    timer = _fn(r"function startSearchStepTimer\(box, step\) \{(.*?)\n\}", "startSearchStepTimer")
    assert "clearSearchStepTimer(box);" in timer, "新 start 前未清旧表（同一 box 只许一个活动计时器）"
    assert "box._spTimer = setInterval(tick, 1000);" in timer, "计时器未挂 box._spTimer 或非每秒节奏"
    assert "`· ${Math.floor((Date.now() - t0) / 1000)}s`" in timer, "文本形态必须是 · Ns，且按起始时刻实算"

    clear = _fn(r"function clearSearchStepTimer\(box\) \{(.*?)\n\}", "clearSearchStepTimer")
    assert "clearInterval(box._spTimer)" in clear, "未 clearInterval（清箱后计时器空转＝泄漏）"
    assert 'box._spTime.textContent = ""' in clear, "终态未清空计时文本（:empty 收起依赖清空）"

    fail_body = _fn(r"function failSearchStream\(form, btn, box, onManualRetry\) \{(.*?)\n\}", "failSearchStream")
    for name, text in (("renderSearchProgress", render), ("failSearchStream", fail_body)):
        stop, wipe = text.index("clearSearchStepTimer(box)"), text.index('box.textContent = ""')
        assert stop < wipe, f"{name} 必须先停表再清箱（顺序反了＝计时器泄漏）"

    assert ".sp-time" in css and ".sp-time:empty" in css, "radar.css 缺 .sp-time 样式或空态收起规则"
