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
    """

    def __init__(self, intent=None, select=None, intent_error=None, select_error=None):
        self._intent = intent
        self._select = select
        self._intent_error = intent_error
        self._select_error = select_error
        self.intent_calls: list[str] = []
        self.select_calls: list[dict] = []

    async def understand_intent(self, query: str, *, prev_intent=None):
        self.intent_calls.append((query, prev_intent))
        if self._intent_error is not None:
            raise self._intent_error
        return self._intent

    async def select_and_reason(self, *, intent_summary, candidates):
        self.select_calls.append(candidates)
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
