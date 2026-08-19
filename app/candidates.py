"""T-032 候选词扫描（《交互流程说明》§15）：池内 topics 词频 → 未命中词表且词频达阈值的词 →
DeepSeek 批量出建议主题（或"不建议收录"）→ 全量覆盖式落库（只留最近一轮）；页面只读展示。

口径（任务书/§15 钉死，勿自由发挥）：
- 触发：jobs 每日链路内、每周补捞之后，仅 UTC 周一（判定在 app.jobs._candidate_scan_due，本模块不重复判）；
- 词频统计：repos.topics（JSON 数组字符串）用 json_each 展开按词计数（dead=0 仓）；
- 未命中判定复用词表归一口径（app.classify 决策 3 v2）：词条精确命中、或词 == 词表词条+"s" 亦命中——
  单向、不做通用去 s（防 css 被剥成 cs 误伤），避免把 agents 这类已归一的词又报成未命中；
- 阈值：POOL_COUNT_THRESHOLD = 5（2026-08-15 实施时依池内真实分布定：live 1353 仓、topics 总词种 4965；
  未命中词数随阈值变化：>=3 → 600、>=5 → 257、>=10 → 83、>=20 → 31；取 5 滤掉约 94% 长尾噪音，
  头部仍保留 claude-code(81)/codex(63)/agent(48)/local-first(24)/mcp-server(21) 等中高频真实信号；
  >=3 的 600 词噪音占比过高（语言名/平台词大量混入，AI 批量判定收益低），>=10 会漏 mcp-server(21)/
  agent-skills(21) 这类正在崛起的新词）；
- AI：批量一次调用（DeepSeekClient.suggest_topics，输入只含候选词与主题 label，不含仓库数据，§15.4）；
  值非主题名且非"不建议收录"的行丢弃（AI 幻觉防御）；解析失败/调用失败 → 整段跳过记日志，
  不写表不清表（页面继续显示上一轮，§15.3）；key 未配置 → INFO 降级跳过（与 ensure_daily_ai 同口径）；
- 落库：DELETE 后 INSERT 全量覆盖（只留最近一轮）+ candidate_scans 单行记录成功扫描
  （区分"扫描过但无候选"与"从未成功扫描"两种空态，§15.3）；
- 红线：扫描结果永不写 config/topics.yaml；AI 输出只展示不参与归类计算（§15.4，决策 12）。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from app.ai import DeepSeekClient
from app.classify import TopicSpec, load_topics
from app.config import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

TOPICS_PATH = BASE_DIR / "config" / "topics.yaml"  # 与 web/ai 层同一路径来源
NOT_RECOMMENDED = "不建议收录"  # AI 判定值（§15.2）：词义宽泛/平台通用词/与现有主题无关
POOL_COUNT_THRESHOLD = 5  # 词频阈值（依据见模块 docstring）

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"  # schema 硬约定：UTC 定长


def _all_words(table: dict[str, TopicSpec]) -> set[str]:
    """词表全部词条拍平为集合（命中判定 O(1) 查询；词表是封板物，扫描期不变）。"""
    return {w for spec in table.values() for w in spec["words"]}


def is_known_term(term: str, table: dict[str, TopicSpec]) -> bool:
    """词是否命中词表（与 classify_topics 同一归一口径，决策 3 v2）：精确命中、或 term == 词条+"s" 亦命中。

    单向：不做反向（词表复数词条不命中单数 topic）、不做通用去 s（防 css 被剥成 cs 之类误伤）——
    与 app.classify.classify_topics 的归一方向逐字对齐，避免 agents 这类已归一的词又被报成未命中。
    """
    words = _all_words(table)
    return term in words or (term.endswith("s") and term[:-1] in words)


def topic_frequencies(conn: sqlite3.Connection) -> dict[str, int]:
    """池内全部 topics 词频（dead=0 仓）：repos.topics 为 JSON 数组字符串，json_each 展开按词计数。

    与采集层入库格式同源（GitHub topics 原值小写 kebab-case）；dead 仓停止采集不参与词频。
    """
    return {
        r["term"]: r["n"]
        for r in conn.execute(
            "SELECT value AS term, COUNT(*) AS n FROM repos, json_each(repos.topics)"
            " WHERE repos.dead = 0 GROUP BY value"
        )
    }


def _replace_round(conn: sqlite3.Connection, rows: list[tuple[str, str, int]], now: datetime) -> None:
    """全量覆盖式落库（§15.2/§15.4：只留最近一轮）+ 单行扫描记录（同一事务）。

    rows = [(term, suggested_topic, pool_count)]；空列表 = 本轮无候选——同样清掉上一轮
    （已封板收进词表/降频的词自动从列表消失，§15.1），并记录成功扫描时刻。
    """
    scanned_at = now.strftime(_ISO_FMT)
    conn.execute("DELETE FROM topic_candidates")
    conn.executemany(
        "INSERT INTO topic_candidates (term, suggested_topic, pool_count, scanned_at) VALUES (?, ?, ?, ?)",
        [(t, s, c, scanned_at) for t, s, c in rows],
    )
    conn.execute(
        "INSERT OR REPLACE INTO candidate_scans (id, scanned_at, term_count) VALUES (1, ?, ?)",
        (scanned_at, len(rows)),
    )
    conn.commit()


async def scan_candidates(
    conn: sqlite3.Connection,
    client: DeepSeekClient,
    *,
    now: datetime | None = None,
    log: logging.Logger | None = None,
) -> dict:
    """执行一轮候选词扫描（§15.2 主路径）：词频统计 → 阈值+未命中过滤 → AI 批量建议 → 全量覆盖落库。

    返回 {"scanned": bool, "frequencies": 池内词种数, "candidates": 达标候选词数,
    "kept": 落库词数, "rejected": AI 判"不建议收录"词数, "dropped": 值非法丢弃词数}；
    scanned=False 表示本轮未执行（key 未配置 / AI 调用失败 / 解析失败）——未写表未清表（页面显示上一轮）。
    """
    log = log or logger
    if not get_settings().ai_api_key:
        log.info("AI 未配置或已禁用（AI_API_KEY/AI_ENABLED）：跳过候选词扫描（降级，页面显示上一轮结果）")
        return {"scanned": False}
    now = now or datetime.now(timezone.utc)
    table = load_topics(TOPICS_PATH)
    topic_names = [spec["label"] for spec in table.values()]
    freq = topic_frequencies(conn)
    # 未命中且达阈值：按词频降序、同频按词名（页面同序）
    terms = sorted(
        (t for t, n in freq.items() if n >= POOL_COUNT_THRESHOLD and not is_known_term(t, table)),
        key=lambda t: (-freq[t], t),
    )
    if not terms:
        _replace_round(conn, [], now)
        log.info(
            "候选词扫描（%s）：无候选（词频全不达标或全部命中词表），已清上一轮并记录扫描时刻",
            now.strftime(_ISO_FMT),
        )
        return {"scanned": True, "frequencies": len(freq), "candidates": 0, "kept": 0, "rejected": 0, "dropped": 0}
    try:
        suggested = await client.suggest_topics(terms, topic_names)
    except Exception as exc:  # 含 DeepSeekAuthError：扫描每周一次，记日志跳过即可（§15.3 扫描异常记日志不报警）
        log.warning("候选词 AI 建议失败：整段跳过不清表（页面显示上一轮），下周自然重试：%s", exc)
        return {"scanned": False}
    valid_values = {*topic_names, NOT_RECOMMENDED}
    rows: list[tuple[str, str, int]] = []
    dropped = 0
    for t in terms:
        value = suggested.get(t)
        if value not in valid_values:
            dropped += 1  # AI 幻觉防御：主题外名字/空值不落库
            continue
        rows.append((t, value, freq[t]))
    rejected = sum(1 for _, s, _ in rows if s == NOT_RECOMMENDED)
    _replace_round(conn, rows, now)
    log.info(
        "候选词扫描（%s）：池内词种 %d、达标候选 %d、AI 判不建议收录 %d、值非法丢弃 %d、落库 %d",
        now.strftime(_ISO_FMT),
        len(freq),
        len(terms),
        rejected,
        dropped,
        len(rows),
    )
    return {
        "scanned": True,
        "frequencies": len(freq),
        "candidates": len(terms),
        "kept": len(rows),
        "rejected": rejected,
        "dropped": dropped,
    }
