"""智能搜索（T-034，§17）：三段式链路——LLM 意图理解 → 池内召回粗排 → LLM 精选；池内不足 10 条时 GitHub Search 实时补足。

口径（spec smart-search 钉死，勿自由发挥）：
- 意图理解：用户自然语言 → 多关键词×多语言×主题词（JSON，语言对齐 classify.LANGUAGES 键集）；
  返回内容非法 JSON → 退化为原输入单关键词（不当场失败）；调用失败/账户类错误 → 搜索暂不可用（fail-loud）；
- 池内召回：alive 仓关键词组匹配（full_name/description_en/topics 任一命中任一关键词计一次命中，
  instr 与 LIKE %kw% 等价且无通配符陷阱），语言过滤取交集；粗排 = 命中数降序 → 最新星数降序，取前 30；
- 精选：候选 30 → 至多 10 条 + 每条约 50 字中文理由（编号引用防幻觉）；非法返回/超范围引用/调用失败
  → 退化按粗排顺序直出池内候选（无推荐理由），记 WARNING；不足 10 条有几个列几个，不凑数；
- 池外补搜：池内精选后不足 10 条才触发，同组关键词（含语言限定）GitHub Search 补足；池外行 external=True，
  展示 GitHub 原始描述、无增星/译文/概要；失败/限速/token 缺失 → 跳过补搜（池内照常，页面标注）；
- 空结果如实说明，不硬编任何条目；候选集永远来自真实池子/GitHub 真实响应。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app.ai import DeepSeekAuthError, DeepSeekClient, DeepSeekError
from app.classify import LANGUAGES
from app.collector.github import normalize_github_created_at
from app.report import _created_year

# 展示/精选上限与候选粗排上限（spec 决策 2/3：粗排取约前 30、精选至多 10 条，不足不凑数）
RESULT_LIMIT = 10
POOL_TOP_N = 30

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Intent:
    """LLM 意图理解结果（或退化形态）：keywords 小写英文关键词、languages 为 LANGUAGES 键集内的
    GitHub 精确语言名（可能空 = 不过滤）、topics 小写主题词（仅供补搜查询参考）；degraded=True
    表示 LLM 内容非法退化为原输入单关键词（spec 口径，页面意图透明行如实展示）。"""

    raw_query: str
    keywords: list[str]
    languages: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    degraded: bool = False

    @property
    def display_text(self) -> str:
        """意图透明行展示文本（"理解为：crawler、scraping ／ 语言：Python"形态；语言空则省略语言段）。"""
        text = "、".join(self.keywords)
        if self.languages:
            text += f" ／ 语言：{'、'.join(self.languages)}"
        return text


@dataclass(frozen=True)
class Candidate:
    """池内召回候选：hits 为命中数（粗排键），stars 为最新快照星数，captured_at 供端点日期标注。"""

    repo_id: int
    full_name: str
    description_en: str | None
    language: str | None
    topics: list[str]
    stars: int
    hits: int
    created_year: int | None
    captured_at: str | None = None


@dataclass(frozen=True)
class PoolHit:
    """池内精选结果：reason None = 精选退化粗排直出（页面行无推荐理由块，如实）。"""

    candidate: Candidate
    reason: str | None


@dataclass(frozen=True)
class ExternalHit:
    """池外补搜结果：展示 GitHub 原始描述，无增星/译文/概要（spec 决策 4）。"""

    full_name: str
    description_en: str | None
    language: str | None
    stars: int | None
    created_year: int | None


@dataclass(frozen=True)
class SearchResult:
    """一次搜索的完整结果：unavailable=True 时其余字段为空（页面渲染"搜索暂不可用"）；external_failed
    表示补搜被跳过/失败（池内结果照常展示，页面池外区如实标注）。"""

    intent: Intent | None = None
    pool_hits: list[PoolHit] = field(default_factory=list)
    external_hits: list[ExternalHit] = field(default_factory=list)
    unavailable: bool = False
    unavailable_reason: str | None = None
    external_failed: bool = False

    @property
    def all_hits(self) -> list[PoolHit | ExternalHit]:
        return [*self.pool_hits, *self.external_hits]


def _normalize_keyword(raw: str) -> str:
    """关键词归一：去首尾空白转小写（GitHub 匹配大小写不敏感；展示与检索同口径）。"""
    return raw.strip().lower()


def _normalize_language(raw: str) -> str | None:
    """语言归一：大小写不敏感匹配 LANGUAGES 键集（GitHub 精确名）；非法值返回 None 由调用方丢弃。"""
    for name in LANGUAGES:
        if name.lower() == raw.strip().lower():
            return name
    return None


def _build_intent(raw_query: str, parsed: Any) -> Intent:
    """LLM 解析结果 → Intent：字段形态/元素类型非法或关键词全空 → 退化原输入单关键词（spec 口径，
    不当场失败）；语言与主题词逐元素归一后丢弃非法值（语言取值对齐 classify.LANGUAGES 键集）。"""
    if not isinstance(parsed, dict):
        return Intent(raw_query, [_normalize_keyword(raw_query)], degraded=True)
    kws = parsed.get("keywords")
    langs = parsed.get("languages")
    topics = parsed.get("topics")
    if not isinstance(kws, list) or not isinstance(langs, list) or not isinstance(topics, list):
        return Intent(raw_query, [_normalize_keyword(raw_query)], degraded=True)
    keywords: list[str] = []
    for k in kws:
        if not isinstance(k, str):
            continue
        norm = _normalize_keyword(k)
        if norm and norm not in keywords:
            keywords.append(norm)
    languages: list[str] = []
    for lang in langs:
        if not isinstance(lang, str):
            continue
        norm = _normalize_language(lang)
        if norm is not None and norm not in languages:
            languages.append(norm)
    topic_words = [t.strip().lower() for t in topics if isinstance(t, str) and t.strip()]
    if not keywords:
        # 无关键词无法检索：退化原输入（LLM 给了空/非法关键词数组也算内容非法）
        return Intent(raw_query, [_normalize_keyword(raw_query)], degraded=True)
    return Intent(raw_query, keywords, languages, topic_words)


def _endpoint_snapshot(conn: sqlite3.Connection, repo_id: int) -> sqlite3.Row | None:
    """取单仓库最新一张快照（主键 (repo_id, captured_at) 索引 seek，与 app.report 同模式，LIMIT 1）。"""
    return conn.execute(
        "SELECT stars, captured_at FROM star_snapshots WHERE repo_id = ? ORDER BY captured_at DESC LIMIT 1",
        (repo_id,),
    ).fetchone()


def _count_hits(row: sqlite3.Row, keywords: list[str]) -> int:
    """命中计数（spec 口径：full_name/description_en/topics 任一命中任一关键词记一次命中）——
    按 (字段 × 关键词) 组合计数（与 SQL 入选条件同构：入选 ⟺ 计数 ≥ 1）；topics 为 JSON 数组字符串。"""
    fields = [row["full_name"], row["description_en"] or "", row["topics"] or ""]
    return sum(1 for f in fields for kw in keywords if kw in f.lower())


def recall_candidates(conn: sqlite3.Connection, intent: Intent) -> list[Candidate]:
    """池内召回（spec 决策 2）：alive 仓关键词组匹配 + 语言过滤交集，按命中数降序 → 最新星数降序取前 30。

    SQL 侧用 instr（子串包含，ASCII 大小写不敏感）与 LIKE '%kw%' 等价，但关键词含 %/_ 时无通配符
    陷阱；Python 侧 _count_hits 同构计数——两处口径一致，入选行必然 hits ≥ 1。
    星数逐仓最新快照 seek（repos 小表量级，每仓一次 LIMIT 1，不碰 star_snapshots 全表扫）。
    """
    keywords = list(dict.fromkeys(intent.keywords))  # 去重保序
    if not keywords:
        return []
    params: list[str] = []
    sql = "SELECT r.id, r.full_name, r.description_en, r.language, r.topics, r.github_created_at FROM repos r WHERE r.dead = 0"
    if intent.languages:
        sql += " AND r.language IN (" + ",".join("?" * len(intent.languages)) + ")"
        params.extend(intent.languages)
    kw_conds = []
    for kw in keywords:
        kw_conds.append(
            "(instr(lower(r.full_name), ?) > 0 OR instr(lower(COALESCE(r.description_en, '')), ?) > 0"
            " OR instr(lower(r.topics), ?) > 0)"
        )
        params.extend([kw] * 3)
    sql += " AND (" + " OR ".join(kw_conds) + ")"
    rows = conn.execute(sql, params).fetchall()
    starred: list[tuple[int, int, sqlite3.Row, sqlite3.Row | None]] = []
    for r in rows:
        snap = _endpoint_snapshot(conn, r["id"])
        stars = snap["stars"] if snap is not None else 0
        starred.append((_count_hits(r, keywords), stars, r, snap))
    starred.sort(key=lambda t: (-t[0], -t[1]))  # 命中数降序 → 星数降序（粗排键钉死）
    candidates: list[Candidate] = []
    for hits, stars, r, snap in starred[:POOL_TOP_N]:
        topics: list[str] = []
        try:
            raw = r["topics"]
            if isinstance(raw, str) and raw.strip():
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    topics = [t for t in parsed if isinstance(t, str)]
        except ValueError:
            pass  # topics 脏数据（采集层保证 JSON，防御性跳过）
        candidates.append(
            Candidate(
                repo_id=r["id"],
                full_name=r["full_name"],
                description_en=r["description_en"],
                language=r["language"],
                topics=topics,
                stars=stars,
                hits=hits,
                created_year=_created_year(r["github_created_at"]),
                captured_at=None if snap is None else snap["captured_at"],
            )
        )
    return candidates


async def _select_with_ai(
    ai_client: DeepSeekClient, intent: Intent, candidates: list[Candidate]
) -> list[PoolHit]:
    """LLM 精选（spec 决策 3）：候选 → 至多 10 条 + 理由，按 LLM 返回的匹配度顺序保持。

    解析失败/超范围编号/理由形态非法 → 整体退化按粗排顺序直出（无理由）记 WARNING——LLM 只许从
    候选清单中按编号选择，任何一个非法引用都说明本轮输出不可信（防幻觉口径，spec 钉死整体退化）。
    DeepSeekAuthError 直通（路由层映射"搜索暂不可用"）；其余调用失败同退化口径（任务书 2.3）。
    """
    if not candidates:
        return []
    summary = f"{intent.raw_query}（关键词：{'、'.join(intent.keywords)}；语言：{'、'.join(intent.languages) or '不限'}）"
    payload = [
        {
            "id": i,
            "full_name": c.full_name,
            "description_en": c.description_en,
            "language": c.language,
            "stars": c.stars,
        }
        for i, c in enumerate(candidates, 1)
    ]

    def fallback(why: str) -> list[PoolHit]:
        logger.warning("智能搜索精选退化粗排直出（无推荐理由）：%s", why)
        return [PoolHit(c, None) for c in candidates[:RESULT_LIMIT]]

    try:
        parsed = await ai_client.select_and_reason(intent_summary=summary, candidates=payload)
    except DeepSeekAuthError:
        raise  # 账户类确定性错误：路由层映射"搜索暂不可用"（与意图理解段同姿态）
    except Exception as exc:  # DeepSeekError（超时/5xx/畸形响应）：任务书 2.3 精选失败退化粗排直出
        return fallback(f"AI 调用失败：{exc}")
    if parsed is None:
        return fallback("AI 响应解析失败")
    results = parsed.get("results")
    if not isinstance(results, list):
        return fallback("响应缺 results 数组")
    by_id = {i: c for i, c in enumerate(candidates, 1)}
    picks: list[tuple[int, str]] = []
    for item in results:
        if not isinstance(item, dict):
            return fallback("results 元素非对象")
        rid, reason = item.get("id"), item.get("reason")
        if not isinstance(rid, int) or rid not in by_id or not isinstance(reason, str) or not reason.strip():
            return fallback(f"超范围编号或理由非法：{item!r}")  # 超范围引用 → 整体退化（spec 钉死）
        if rid in {p[0] for p in picks}:
            return fallback(f"编号重复引用：{rid}")
        picks.append((rid, reason.strip()))
    return [PoolHit(by_id[rid], reason) for rid, reason in picks[:RESULT_LIMIT]]


def _github_query(intent: Intent) -> str:
    """补搜查询拼接（spec 决策 4：同组关键词含语言限定；sort=stars 由调用方传）。

    关键词 OR 组合（与池内召回"任一命中"同口径）；含冒号的 token 丢弃——GitHub 限定词都是 key:value
    形态，关键词里带冒号会改变查询语义（LLM 产出一般不会，防御性过滤）；多语言/多关键词用括号分组，
    避免 GitHub 的 OR/AND 混用优先级把语言限定并进 OR 链失效。
    运算符预算：GitHub Search 单查询限 5 个 AND/OR/NOT（超限 422，k3 评审 F2-1）——关键词取前 4、
    语言取前 2，最坏 3+1=4 个 OR 留 1 个余量；截断仅影响补搜覆盖面，池内召回不受影响。
    """
    kws = [k for k in intent.keywords if k and ":" not in k][:4]
    parts: list[str] = []
    if kws:
        parts.append(f"({' OR '.join(kws)})" if len(kws) > 1 else kws[0])
    if intent.languages:
        lang_list = [f'language:"{lang}"' for lang in intent.languages[:2]]
        parts.append(f"({' OR '.join(lang_list)})" if len(lang_list) > 1 else lang_list[0])
    return " ".join(parts)


def _external_hit(item: dict) -> ExternalHit:
    """GitHub Search item → 池外行（REST 响应字段与采集层同构；created_at 归一化后取年份）。"""
    stars = item.get("stargazers_count")
    return ExternalHit(
        full_name=item.get("full_name"),
        description_en=item.get("description"),
        language=item.get("language"),
        stars=stars if isinstance(stars, int) else None,
        created_year=_created_year(normalize_github_created_at(item.get("created_at"))),
    )


async def search_external(
    conn: sqlite3.Connection, github_client: Any, intent: Intent, need: int
) -> list[ExternalHit]:
    """池外实时补搜（spec 决策 4）：GitHub Search 按星数降序补足 need 条；已在池（含刚关注入池）排除。

    单次搜索至多 2 次 Search 调用（per_page=100，限速 30 次/分）；补不满按实际返回，不凑数。
    异常（限速/5xx/token 缺失等）全部上抛，由 run_search 统一降级（跳过补搜、池内照常、页面标注）。
    """
    if need <= 0:
        return []
    in_pool = {r["full_name"] for r in conn.execute("SELECT full_name FROM repos")}
    query = _github_query(intent)
    if not query:
        return []
    hits: list[ExternalHit] = []
    seen: set[str] = set()
    for page in (1, 2):  # 至多 1~2 次 Search 调用（spec 决策 4 钉死）
        data = await github_client.search_repositories(query, sort="stars", per_page=100, page=page)
        for item in data.get("items") or []:
            full_name = item.get("full_name")
            if not isinstance(full_name, str) or full_name in in_pool or full_name in seen:
                continue
            seen.add(full_name)
            hits.append(_external_hit(item))
            if len(hits) >= need:
                return hits
    return hits


async def run_search(
    conn: sqlite3.Connection,
    ai_client: DeepSeekClient,
    github_client: Any,
    raw_query: str,
    *,
    log: logging.Logger | None = None,
) -> SearchResult:
    """完整检索链路（spec 决策 1/6）：意图理解 → 池内召回粗排 → LLM 精选 →（不足 10）池外补搜。

    降级姿态：
    - 意图理解段——DeepSeekAuthError（key 未配置/无效/余额）与调用失败 → 搜索暂不可用（fail-loud，
      spec AI 降级姿态，不影响榜单主链路）；响应内容非法 JSON → 退化为原输入单关键词继续池内检索；
    - 精选段——AuthError 直通不可用；其余失败 → 粗排直出（无理由）记 WARNING；
    - 补搜段——任何异常 → 跳过补搜，external_failed=True（池内结果照常，页面如实标注）。
    """
    log = log or logger
    try:
        parsed = await ai_client.understand_intent(raw_query)
    except DeepSeekAuthError as exc:
        return SearchResult(unavailable=True, unavailable_reason=str(exc))
    except DeepSeekError as exc:
        log.warning("智能搜索意图理解调用失败，搜索暂不可用：%s", exc)
        return SearchResult(unavailable=True, unavailable_reason=str(exc))
    if parsed is None:
        log.warning("智能搜索意图理解返回非法 JSON，退化为原输入单关键词：%r", raw_query)
        intent = Intent(raw_query, [_normalize_keyword(raw_query)], degraded=True)
    else:
        intent = _build_intent(raw_query, parsed)

    candidates = recall_candidates(conn, intent)
    try:
        pool_hits = await _select_with_ai(ai_client, intent, candidates)
    except DeepSeekAuthError as exc:
        return SearchResult(unavailable=True, unavailable_reason=str(exc))

    external_hits: list[ExternalHit] = []
    external_failed = False
    if len(pool_hits) < RESULT_LIMIT:
        try:
            external_hits = await search_external(
                conn, github_client, intent, RESULT_LIMIT - len(pool_hits)
            )
        except Exception as exc:  # 限速/5xx/token 缺失等：跳过补搜，池内照常（spec 决策 4）
            log.warning("智能搜索池外补搜失败，跳过补搜（仅展示池内结果）：%s", exc)
            external_failed = True
    return SearchResult(
        intent=intent,
        pool_hits=pool_hits,
        external_hits=external_hits,
        external_failed=external_failed,
    )
