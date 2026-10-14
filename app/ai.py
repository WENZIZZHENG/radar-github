"""AI 服务（T-011→T-018）：范围内翻译＋三口径分维度推荐语生成＋全程降级（共识 §7 v4 / 决策 5 v2 / 决策 6 v2）。

口径（任务书钉死，勿自由发挥）：
- 范围集 S（T-017 收窄，v3 全池口径作废；T-018 起周/季榜集 = 主榜 Top50 ∪ 新崛起区 Top10；T-028 主榜 30→50；
  T-033 起三口径榜集再 ∪ 新项目区 Top20）：
  三口径榜去重 ∪ 关注集——
  S 之外的仓库永远不译不生成（已译译文保留不清除；新上榜/新关注仓由每日 job 自动补译，自愈）；
- 翻译段：只译 S 内 description_zh IS NULL 的仓（英文非空、不含 CJK 逐条翻译回填 repos.description_zh）；
  原文变更时采集层已清 description_zh（discover.py 变更检测连带清推荐语），当日本轮自然重译；
  手动单个翻译 API 不受范围限（任何池内仓可手动触发）；
- 推荐语三维度（输入含 README 正文截断，拉取失败/空退化元数据输入、不持久化；按
  (repo_id, dimension, period_label) 写入 recommendations）：
  * week：(repo_id, 'week', 当周标签) 缺失则生成（同周已存在跳过，幂等），输入带本周增量语境；
  * quarter：(repo_id, 'quarter', 当季标签) 缺失或其 generated_week ≠ 当周且跑批日在半月窗口内
    → 生成/REPLACE（每月 1 号、15 号重生；季内其余日期跳过已有行）；
  * total：(repo_id, 'total', 'all') 缺失或跑批日在半月窗口内且 README blob sha 变化 → 生成/REPLACE
    （每月 1 号、15 号比对 sha 决定是否重生，非窗口日不拉 README 不比 sha；总星文本懒口径不每周重刷；
    prompt 不引用具体星数/排名数字，防 evergreen 数字陈旧）；README 拉取失败（sha 未取到，含 404/auth 停拉）
    不触发重生、保留旧行旧指纹——防失败制造每日 churn（README 被删除的 404 场景因此不再触发，属探测能力边界）；
  * 概要（T-024，§11）：与推荐语并存——推荐语＝为什么值得关注（营销视角），概要＝是什么
    （README 文档视角、段落级 3~5 句中文、无维度概念、全页面同一条）；key=(repo_id, 'summary', 'all')，
    覆盖 S 全集（上榜∪关注，同 T-017 推荐语 S 集口径）；懒生成（缺时每日 ensure 补）＋半月窗口内 README 变更
    重生（同 total 段 F2-1 守卫，非窗口日对已有行不拉 README）；仅每日 ensure（refresh=True）执行，
    无任何手动入口（手动批量 worker refresh=False 整段跳过——§11 无手动按钮）；
    prompt 不引用任何星数/增星/排名数字；
  * README sha 比对仅每日 ensure 且在半月窗口日对 S 内仓进行；sha NULL 仓后续出现 README 视为变更自愈；
    周/季维度不随 README 触发；
  * source 列（local-ai-relay）：'ai'＝本模块自动路径写入（缺省）/ 'manual'＝本地回填通道
    （app/local_ai.py）写入；'manual' 行不受半月窗口重生影响——quarter/total/summary 三处窗口守卫遇
    人工行直接跳过（不拉 README、不比 sha、不调用 AI、不覆盖），缺失补缺不受影响，周榜每期次新行也不受影响；
- 降级：DEEPSEEK_API_KEY 未配置 → 记 INFO 返回零统计，服务照常；单条 translate/recommend 失败 →
  记 WARNING 跳过该条计入统计，绝不抛出；DeepSeekAuthError（key 无效/余额/账户权限 401/402/403）
  直通整轮 handler；403 内容审核拦截（content_policy）属单仓失败，按 DeepSeekError 跳过该条；
  GitHub token 缺失/无效 → README 全量退化（readme_sha 保持 NULL），不报错；
  AI 整段异常由调用方（daily_job）吞掉记 ERROR——任何情况下快照主流程不受影响。
- 流式变体（T-041，§17.5；ai-langchain-refactor 后走 LangChain）：`understand_intent`/`select_and_reason`
  的可选 `thinking_cb` 为 None（缺省）时走同步 ainvoke（协议参数与旧实现一致：带 response_format、
  非流式），非 None 时走 astream 并把 `additional_kwargs["reasoning_content"]` 增量逐段喂回调
  （字段缺/空 → 零增量不报错，退化为纯阶段进度）；content 增量只累积、末尾一次性解析（第二道防线
  `_parse_suggested_topics` 不动）。交互路径零自动重试（首字节前失败也不重试，失败如实抛出）。
  流式形态的 response_format 经 `extra_body` 下发（裁定 1）：顶层形式会命中引擎结构化输出解析路径，
  逐段思维链被丢弃只在流末聚合透出；extra_body 形式线上请求体不变（顶层仍带该参数）而逐段恢复。
- 引擎与重试双口径（ai-langchain-refactor 决策 1/2/4/5）：内部实现＝LangChain ChatDeepSeek（单点 factory
  `_build_chat_model` 产交互/批量双实例，均 max_retries=0 关引擎内置重试）；交互路径不包重试，批量路径
  外层 `with_retry(stop_after_attempt=max_retries+1, retry_if_exception_type=RETRYABLE_EXC)`（零等待）——
  可重试类＝超时/连接/5xx/流式空闲超时/响应畸形，429 与其余 4xx、账户类永不重试。连接总上限走
  `AI_TIMEOUT_SECONDS`（缺省 300s），流式空闲超时 `STREAM_CHUNK_TIMEOUT_SECONDS`（60s 写死）。
  对外异常类型与人话文案逐字不变，唯一例外＝交互路径"重试 N 次后放弃"类文案消失（刻意行为变化，
  见 openspec/changes/ai-langchain-refactor）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import NoReturn

import httpx
from langchain_core.runnables import RunnableLambda
from langchain_deepseek import ChatDeepSeek
from langchain_openai.chat_models._client_utils import StreamChunkTimeoutError
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
)

from app.classify import LANGUAGES, load_topics
from app.collector.github import GitHubAuthError, GitHubClient
from app.config import (
    BASE_DIR,
    DEFAULT_AI_BASE_URL,
    DEFAULT_AI_MAX_RETRIES,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_README_HEAD_CHARS,
    DEFAULT_AI_TIMEOUT_SECONDS,
    get_settings,
)
from app.report import ReportRow, RisingRow, compute_boards, pool_days_label

CHAT_URL = DEFAULT_AI_BASE_URL  # 缺省端点别名；实际请求走实例 base_url（.env AI_BASE_URL 可换 OpenAI 兼容提供方）
CHAT_MODEL = DEFAULT_AI_MODEL  # 缺省模型别名；实际请求走实例 model（.env AI_MODEL）
TOPICS_PATH = BASE_DIR / "config" / "topics.yaml"  # 与 web 层同一路径来源（app/web/routes.py TOPICS_PATH）

DEFAULT_MAX_RETRIES = DEFAULT_AI_MAX_RETRIES  # 缺省重试次数别名；批量路径重试次数（.env AI_MAX_RETRIES）
REQUEST_TIMEOUT_SECONDS = DEFAULT_AI_TIMEOUT_SECONDS  # 缺省超时别名；实际走实例 request_timeout（.env AI_TIMEOUT_SECONDS）
STREAM_CHUNK_TIMEOUT_SECONDS = 60.0  # 流式空闲超时：60 秒无 chunk 即判挂（引擎缺省 120s 不沿用，决策 5）

# README 正文截断入 prompt 的缺省字符上限（T-017 本人拍板授权实施）：≈2000~3000 tokens，
# 覆盖 README 头部核心信息且不撑爆上下文；截断点取前部（README 惯例：开头即项目定位）。
# 缺省值别名；实际截断走 _ReadmeState 实例 max_chars（.env AI_README_HEAD_CHARS——单次调用成本主杠杆）
README_HEAD_CHARS = DEFAULT_AI_README_HEAD_CHARS

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"  # schema 硬约定：UTC 定长（与 app.report 同口径）

# CJK 统一表意文字命中即视为已有中文（含中英混排简介）：原文已有中文不再送译
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 按 full_name 反查 repo_id 的分块大小：防御旧编译 SQLite 的 999 变量上限（与 app.report 同口径）
_NAME_LOOKUP_CHUNK = 500

logger = logging.getLogger(__name__)


class DeepSeekError(RuntimeError):
    """DeepSeek 调用失败的基类：单条失败由 ensure_daily_ai 捕获跳过，不阻断整轮。"""


class DeepSeekAuthError(DeepSeekError):
    """API key 缺失/无效及账户类确定性错误（401 key 无效、402 余额不足、403 账户权限）：逐条重试无意义，直通整轮 handler（与 GitHubAuthError 同姿态）。

    注：403 中含 content_policy 标记的内容审核拦截属单仓确定性失败，不归此类，按 DeepSeekError 跳过单条。
    """


class _ResponseMalformedError(DeepSeekError):
    """响应畸形（响应内容缺失/空白，或引擎解析层拒收响应体）：对外仍是 DeepSeekError（文案"响应畸形"逐字保留）。

    子类化的唯一用途＝进 RETRYABLE_EXC 白名单，复刻"畸形重试一次"的批量口径（决策 2-4）；
    交互路径不包 with_retry，故直接抛出。
    """


# 批量路径可重试异常白名单（决策 2-3）：超时/连接/5xx/流式空闲超时/响应畸形。
# 429 与其余 4xx、账户类（401/402/403）永不入列——盲重试只会放大调用量（既有拍板延续）。
# 注：openai 3.3.1 无 BadGatewayError/ServiceUnavailableError/GatewayTimeoutError 三个类（已核），
# 5xx 统一由 InternalServerError 覆盖——LangChain 包装的 OpenAIAPIError 亦继承之，502/503/504 实测落此类。
RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    StreamChunkTimeoutError,
    _ResponseMalformedError,
)


def _build_chat_model(
    *,
    api_key: str,
    base_url: str,
    model: str,
    request_timeout: float,
    http_async_client: httpx.AsyncClient | None,
) -> ChatDeepSeek:
    """单点 factory（决策 1）：facade 参数 → ChatDeepSeek 构造参数映射。

    base_url→api_base、model→model、request_timeout→request_timeout、transport 包出的 httpx 客户端
    →http_async_client（测试缝保留：MockTransport 收到的请求 URL 仍为 {base_url}/chat/completions）；
    max_retries=0 关引擎/SDK 内置重试（其语义＝重试 429＋指数退避，与本仓口径冲突，不关则双重复试放大
    调用量）；temperature/response_format 不进构造器，由方法调用点经 .bind() 下发（spike 实证到达请求体）。
    """
    return ChatDeepSeek(
        # SDK 构造会校验空 key；用占位符绕过，真实"空 key"拦截保留在使用点（_require_api_key），
        # 与原有语义一致（config 层不报错，调用第一刀才报错）。
        api_key=api_key or " ",
        api_base=base_url,
        model=model,
        request_timeout=request_timeout,
        max_retries=0,
        stream_chunk_timeout=STREAM_CHUNK_TIMEOUT_SECONDS,
        http_async_client=http_async_client,
    )


def _is_content_policy_403(exc: PermissionDeniedError) -> bool:
    """判定 403 是否来自上游内容审核拦截（命中 content_policy 子串即认定，大小写不敏感）。"""
    marker = "content_policy"
    haystack = " ".join(
        str(part)
        for part in (
            getattr(exc, "message", ""),
            exc,
            getattr(exc, "body", ""),
        )
    )
    return marker in haystack.lower()


def _chunk_text(chunk: object) -> str:
    """从一次流式 chunk 取正文增量文本：content 为 str 直取；list 形态（content blocks）拼其中文本块；
    其余形态返回空串（不报错）。"""
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _reasoning_delta(chunk: object) -> str:
    """从一次流式 chunk 取思维链增量（T-041，§17.5）。

    只认 `additional_kwargs["reasoning_content"]` 字符串增量——ChatDeepSeek 唯一透出点（spike 实证，
    data/ai-fw-spike/result.json）；`reasoning_details` 数组兼容随旧手搓实现整体退役（引擎不解析该
    字段，属死代码）。字段缺/空 → 空串（调用方不产生思维链增量，退化为纯阶段进度）。
    """
    kwargs = getattr(chunk, "additional_kwargs", None)
    if not isinstance(kwargs, dict):
        return ""
    raw = kwargs.get("reasoning_content")
    return raw if isinstance(raw, str) else ""


def _require_content(message: object) -> str:
    """响应自检：取正文（str 或 text 块拼接）去首尾空白，空/缺 → `_ResponseMalformedError`。

    文案与旧实现逐字一致（"DeepSeek 响应畸形（JSON 无法解析或缺 content）"＋响应摘录）；批量路径
    该异常进 RETRYABLE_EXC 白名单 → 复刻"畸形重试一次"，交互路径直接抛。
    """
    text = _chunk_text(message).strip()
    if not text:
        raise _ResponseMalformedError(f"DeepSeek 响应畸形（JSON 无法解析或缺 content）：{repr(message)[:200]}")
    return text


# 引擎解析层拒收响应体时实际抛出的异常类型（F2-1 修复的捕获面，2026-10-14 逐例取证）：
# 无 response_format → LangChain 响应类型检查抛 ValueError；带 response_format（结构化输出解析面）
# → openai SDK `_parse_chat_completion` 遍历 str 的 `.choices` 抛 AttributeError、200＋{"error": …} 体
# 抛 TypeError。三型共同语义＝"引擎未能把响应体解释成一条可用的助手消息"，与旧实现的"响应畸形"同类。
_ENGINE_PARSE_ERRORS = (ValueError, AttributeError, TypeError)


def _malformed_engine_response(exc: Exception) -> _ResponseMalformedError:
    """引擎解析层拒收响应体 → "响应畸形"对外异常（F2-1 语义恢复，2026-10-14 主 agent 裁定）。

    旧实现此类响应落"DeepSeek 响应畸形（…）"文案＋批量路径白名单重试一次；引擎切换后同样的响应在引擎
    内部就抛异常、落"未知异常"分支（文案变"请求失败（…）"且批量不再重试），双重漂移。归一后同时恢复。
    捕获面仅限引擎调用最小面（调用点已把本模块自身的参数构造移出该面；网络层失败是
    APIConnectionError/APITimeoutError 等特定类型，不在此列）。
    """
    return _ResponseMalformedError(f"DeepSeek 响应畸形（{type(exc).__name__}: {exc}）")


def has_cjk(text: str) -> bool:
    """命中任一 CJK 汉字即视为已有中文，跳过翻译。"""
    return _CJK_RE.search(text) is not None


def _week_label(d: date) -> str:
    """date → ISO 周标签（%G-W%V 定宽零填充，字符串比较即时间序）。

    与 app/web/routes.py 的 _week_label 同一口径（d.isocalendar()，2026-08-09 → "2026-W32"）；
    本模块不 import web 层（职责边界：web 只读 AI 表，AI 不依赖 web），在此内联同构实现。
    """
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _quarter_label(d: date) -> str:
    """date → 季度标签（2026-Q3）；与 app/web/routes.py 的 _quarter_label 同一口径（内联同构）。"""
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def _refresh_window_open(d: date) -> bool:
    """AI 推荐语/概要重生半月窗口：每月 1 号、15 号才允许对已有行执行重生（quarter/total/summary）。

    窗口判定只读 date，不引入新依赖；周榜（week）与手动批量补缺（refresh=False）不受此窗口影响。
    """
    return d.day in (1, 15)


def _parse_suggested_topics(content: str) -> dict | None:
    """解析 suggest_topics 返回的 JSON 对象（T-032，容错）：剥 markdown 代码围栏后取首尾大括号间文本解析；
    无大括号/JSON 畸形/非对象 → None（调用方视为 AI 调用失败整段跳过，§15.3）。"""
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(stripped[start : end + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


# ---------- prompt 构造（local-ai-relay 抽取为模块级纯函数） ----------
# 三条口径的唯一实现：DeepSeekClient 对应方法与本地导出（app/local_ai.py）共用同一份，
# 保证"导出的任务与线上 AI 生成的一模一样"；改动 prompt 文本即同时改两条路径，不做第二套。


def build_translate_prompt(text: str) -> tuple[str, str, float]:
    """简介翻译 prompt（返回 system/user/temperature；user 即待译原文）。"""
    system = (
        "你是开源项目简介翻译器。把用户给出的 GitHub 仓库英文简介翻译成自然简洁的中文，"
        "只输出译文本体：不要加引号包裹，不要加“翻译：”等任何前缀，不要解释。"
        "项目名、品牌名、技术专有名词保留原文。"
    )
    return system, text, 0.2  # 低温度：翻译求稳不求花


def build_recommend_prompt(
    *,
    full_name: str,
    description: str,
    language: str,
    categories: list[str],
    dimension: str,
    delta: int | None = None,
    stars: int | None = None,
    readme: str | None = None,
    pool_days: float | None = None,
) -> tuple[str, str, float]:
    """维度感知推荐语 prompt（T-017，返回 system/user/temperature）。

    输入行：仓库/简介/主语言，readme 非空时附 README 要点（调用方已截断，本函数不重复截断）；
    week/quarter 再附当期增星（delta 非空）与总星数（stars 非空）与上榜分类，total 只附上榜分类。
    prompt 两条钉死口径（本人拍板）：周/季输入必须带当期增量（"本周/本季新增 X 星，为什么火"）；
    total 维度不引用任何具体星数/排名数字（数字由页面行内数据展示，防 evergreen 陈旧）。
    分化钉死（2026-08-12 本人复验反馈）：周/季有增量时必须明确写出当期增星数字——与 total
    "不引用数字"形成肉眼可见的稳定差异（两套文本不再"看起来一样"）。
    T-018 分化：pool_days 非 None（新崛起区仓）时增量语境为"入池 N 天新增"（输入行与钉死句
    同构分化），主榜仓措辞一字不动。
    """
    if dimension not in ("week", "quarter", "total"):
        raise ValueError(f"dimension 非法：{dimension!r}")
    lines = [f"仓库：{full_name}", f"简介：{description}", f"主语言：{language}"]
    if readme:
        lines.append(f"README 要点：\n{readme}")
    if dimension == "total":
        user = "\n".join(lines + [f"上榜分类：{'、'.join(categories)}"])
        system = (
            "你是技术雷达的编辑，为一位资深开发者读者写 GitHub 项目的推荐理由。"
            "根据给出的仓库信息写 2~3 句中文推荐语：第一句说清项目是做什么的，"
            "其余说明它在所属领域中的地位（存量语境，写给长期关注的人看，不追热点）。"
            "不要引用任何具体数字（星数、增星、排名）——数字由页面行内数据展示。"
            "只输出推荐语本体：不要加引号包裹，不要加“推荐理由：”等前缀，不要用列表或标题。"
        )
    else:
        rising_days = pool_days_label(pool_days) if pool_days is not None else None
        delta_word = "本周" if dimension == "week" else "本季"
        if delta is not None:
            lines.append(
                f"入池 {rising_days} 天新增星数：{delta}" if rising_days is not None else f"{delta_word}新增星数：{delta}"
            )
        if stars is not None:
            lines.append(f"总星数：{stars}")
        lines.append(f"上榜分类：{'、'.join(categories)}")
        user = "\n".join(lines)
        board_word = "周榜" if dimension == "week" else "季榜"
        # 分化钉死（2026-08-12 本人复验反馈）：增星语境必须写出具体数字——与 total 维度
        # "不引用任何数字"形成肉眼可见的稳定差异；delta 缺席（历史期次无增量行）时退回软要求
        if rising_days is not None:
            # T-018 分化：新区仓增量语境是"入池 N 天新增"（与主榜"本周/本季新增"同构分化，主榜措辞一字不动）
            why = (
                f"其余说明为什么入池 {rising_days} 天值得关注：必须明确写入池 {rising_days} 天新增星数（{delta} 星）"
                "这个数字，并结合总星数与上榜分类分析增长背后的原因；"
                if delta is not None
                else f"其余说明为什么入池 {rising_days} 天值得关注（结合入池 {rising_days} 天增星、总星数与上榜分类）；"
            )
        else:
            why = (
                f"其余说明为什么{delta_word}值得关注：必须明确写出{delta_word}新增星数（{delta} 星）"
                "这个数字，并结合总星数与上榜分类分析增长背后的原因；"
                if delta is not None
                else f"其余说明为什么{delta_word}值得关注（结合{delta_word}增星、总星数与上榜分类）；"
            )
        system = (
            f"你是技术雷达的编辑，为一位资深开发者读者写 GitHub {board_word}上榜项目的推荐理由。"
            f"根据给出的仓库信息写 2~3 句中文推荐语：第一句说清项目是做什么的，"
            f"{why}有选型参考价值时点明。"
            "只输出推荐语本体：不要加引号包裹，不要加“推荐理由：”等前缀，不要用列表或标题。"
        )
    return system, user, 0.3  # 低温度：推荐语允许一点措辞空间，但不许发散


def build_summary_prompt(
    *,
    full_name: str,
    description: str,
    language: str,
    readme: str | None = None,
) -> tuple[str, str, float]:
    """AI 概要 prompt（T-024，§11，返回 system/user/temperature）。

    prompt 三条钉死口径（本人拍板）：不引用任何星数/增星/排名数字（防 evergreen 陈旧，
    与 total 推荐语同理）；只输出概要本体（无前缀无列表无标题，与 recommend 同风格约束）；
    temperature 0.3。README 正文截断入输入的口径与 recommend 一致（调用方 _ReadmeState
    已按 README_HEAD_CHARS 截断，本函数不重复截断）。
    """
    lines = [f"仓库：{full_name}", f"简介：{description}", f"主语言：{language}"]
    if readme:
        lines.append(f"README 要点：\n{readme}")
    system = (
        "你是技术雷达的编辑，为一位资深开发者读者写 GitHub 项目的 AI 概要。"
        "根据给出的仓库信息，从 README 文档视角写一段 3~5 句的中文概要：这个项目是什么、"
        "由什么组成（核心模块/组件）。"
        "不要引用任何具体数字（星数、增星、排名）——数字由页面行内数据展示。"
        "只输出概要本体：不要加引号包裹，不要加“AI 概要：”等前缀，不要用列表或标题。"
    )
    return system, "\n".join(lines), 0.3  # 低温度：概要求准不求发散（与推荐语同值）


class DeepSeekClient:
    """AI 调用唯一入口（规格 ai-client）：外壳与公开签名稳定，内部实现＝LangChain ChatDeepSeek 双实例。

    交互实例（understand_intent/select_and_reason——流式与同步降级形态同口径）零自动重试；批量实例
    （translate/recommend/summarize/suggest_topics）按 RETRYABLE_EXC 白名单重试 self._max_retries 次。
    transport 可注入（单测 MockTransport 离线跑）；sleep 参数签名保留但已无消费点（重试零等待，决策 2）。
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = CHAT_URL,
        model: str = CHAT_MODEL,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._api_key = api_key
        self._max_retries = max_retries
        self._sleep = sleep  # 签名保留（注入面不变）；重试零等待后不再被调用（决策 2 存疑点 1 拍板）
        # transport 注入时，把 transport 包进 httpx.AsyncClient 再作为 http_async_client 传给引擎，
        # 单测 MockTransport handler 收到的请求 URL 保持为 {base_url}/chat/completions。
        self._http_client = (
            httpx.AsyncClient(transport=transport, timeout=request_timeout) if transport is not None else None
        )
        common = {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "request_timeout": request_timeout,
            "http_async_client": self._http_client,
        }
        # 双实例（决策 1）：交互＝零自动重试；批量＝"引擎调用＋响应自检"一并进 with_retry 白名单重试
        # （响应畸形是外壳自检产物，须在包装内才走得上"畸形重试一次"的既有口径，决策 2-4）。
        self._interactive = _build_chat_model(**common)
        self._batch_model = _build_chat_model(**common)
        # 重试单元＝"引擎最小调用面（含解析层异常归一）＋响应自检"：两者都须在 with_retry 包装内，
        # 才走得上"畸形重试一次"的既有口径（决策 2-4）；归一语义见 _malformed_engine_response（F2-1）。
        self._batch = RunnableLambda(self._batch_once).with_retry(
            stop_after_attempt=max_retries + 1,
            retry_if_exception_type=RETRYABLE_EXC,
            wait_exponential_jitter=False,  # 零等待立即重试（不引 1s 薄包装，决策 2 存疑点 1 拍板）
        )

    async def aclose(self) -> None:
        for chat_model in (self._interactive, self._batch_model):
            await chat_model.root_async_client.close()
        if self._http_client is not None:
            await self._http_client.aclose()

    async def __aenter__(self) -> DeepSeekClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def translate(self, text: str) -> str:
        """英文简介 → 中文译文本体（一条一次；prompt 见模块级 build_translate_prompt）。"""
        system, user, temperature = build_translate_prompt(text)
        return await self._batch_chat(system=system, user=user, temperature=temperature)

    async def recommend(
        self,
        *,
        full_name: str,
        description: str,
        language: str,
        categories: list[str],
        dimension: str,
        delta: int | None = None,
        stars: int | None = None,
        readme: str | None = None,
        pool_days: float | None = None,
    ) -> str:
        """维度感知推荐语（T-017）：周/季增量语境（输入含当期增量）；总星存量语境"是什么＋领域地位"。

        prompt 文本与口径见模块级 build_recommend_prompt（本地导出共用同一份，防两条路径漂移）；
        本方法只负责调用。
        """
        system, user, temperature = build_recommend_prompt(
            full_name=full_name,
            description=description,
            language=language,
            categories=categories,
            dimension=dimension,
            delta=delta,
            stars=stars,
            readme=readme,
            pool_days=pool_days,
        )
        return await self._batch_chat(system=system, user=user, temperature=temperature)

    async def summarize(
        self,
        *,
        full_name: str,
        description: str,
        language: str,
        readme: str | None = None,
    ) -> str:
        """项目 AI 概要（T-024，§11）：README 文档视角"这个项目是什么/由什么组成"。

        与推荐语并存不混淆：推荐语＝为什么值得关注（营销视角，三维度分榜）；概要＝是什么
        （文档视角，段落级 3~5 句中文，无维度概念，全页面同一条）。
        prompt 文本与三条钉死口径见模块级 build_summary_prompt（本地导出共用同一份）；本方法只负责调用。
        """
        system, user, temperature = build_summary_prompt(
            full_name=full_name, description=description, language=language, readme=readme
        )
        return await self._batch_chat(system=system, user=user, temperature=temperature)

    async def suggest_topics(self, terms: list[str], topic_names: list[str]) -> dict[str, str]:
        """候选词 → 建议主题（T-032，§15.2）：批量一次调用返回 {词: 主题名或"不建议收录"}。

        - 输入只含候选词与词表现有主题 label（§15.4 红线：不含任何仓库数据）；
        - 输出 JSON 对象（键为候选词原样），markdown 围栏等杂质容错后解析；
        - 解析失败抛 DeepSeekError——调用方按"AI 调用失败"整段跳过（不写表不清表，
          页面显示上一轮，§15.3）；DeepSeekAuthError 直通（同其他方法）；
        - 值合法性（主题名枚举/"不建议收录"）由调用方过滤，本方法只负责调用与解析。
        """
        system = (
            "你是技术雷达的编辑。下面给出 GitHub 仓库上的候选主题标签（topics）清单与现有主题清单。"
            "请为每个候选标签判断应归入哪个现有主题（给出主题名）；若该标签词义宽泛、属平台/通用词"
            "或与现有主题无关，则写“不建议收录”。"
            "只输出一个 JSON 对象：键为候选标签原样，值为主题名或“不建议收录”，不要输出其他内容。"
        )
        user = "候选标签：\n" + "\n".join(f"- {t}" for t in terms) + "\n\n现有主题：\n" + "、".join(topic_names)
        content = await self._batch_chat(  # 低温度：归类求稳不求发散
            system=system,
            user=user,
            temperature=0.2,
            response_format={"type": "json_object"},  # 协议层 JSON 约束（第一道防线）
        )
        parsed = _parse_suggested_topics(content)
        if parsed is None:
            raise DeepSeekError(f"候选主题建议响应解析失败（期望 JSON 对象）：{content[:200]}")
        return parsed

    async def understand_intent(
        self,
        query: str,
        *,
        prev_intent: dict | None = None,
        thinking_cb: Callable[[str], None] | None = None,
    ) -> dict | None:
        """智能搜索意图理解（T-034→T-036，§17）：用户自然语言 → 结构化查询 JSON（多关键词×多语言×主题词×filters×unsupported）。

        输出 JSON 契约：{"keywords": [...], "languages": [...], "topics": [...], "filters": {...}, "unsupported": [...]}——
        keywords 小写英文关键词（3~6 个），languages 取值对齐 classify.LANGUAGES 键集（GitHub 精确名，
        如 "Python"），topics 小写 kebab-case（可空），filters 白名单只含 created_within_days（创建至今天数上限，
        正整数）与 min_stars（最新星数下限，正整数），unsupported 为白名单外/无法检索的条件字符串数组（仅展示不影响检索）。
        追问时传入 prev_intent（上一轮意图的 JSON，含全部字段），prompt 携带旧意图与本轮新输入，要求 LLM 产出合并后的
        新意图（同一 JSON 契约）——如追加关键词、替换语言、filters 同名覆盖/未提及保留等。
        返回 None = 响应内容解析不出合法 JSON 对象（spec 口径：调用方退化为原输入/本轮输入单关键词，不当场失败）；
        调用失败抛 DeepSeekError/DeepSeekAuthError（spec 口径：调用方按"搜索暂不可用"处理）——
        None 与异常的分界即"有响应但内容烂"与"根本调不动"的分界。

        thinking_cb（T-041，§17.5）：None（缺省）走同步 ainvoke（协议参数与旧实现一致：带 response_format、
        非流式）；非 None 走 astream 并把思维链增量喂回调——prompt/response_format/解析与返回契约一字不动。
        两形态同属交互路径：零自动重试，失败如实抛出（新版口径，见模块头）。
        """
        # 语言枚举从 classify.LANGUAGES 动态拼接（给 LLM 的是 GitHub 精确名＝dict 键集，非榜单 key）：
        # prompt 与语言分类单一事实源保持同步，语言榜扩容时意图理解面自动跟随（spec R4 派生一致性）
        lang_names = "/".join(LANGUAGES)
        if prev_intent is None:
            system = (
                "你是 GitHub 项目雷达的搜索意图理解器。把用户的中文自然语言搜索需求翻译成结构化查询。"
                "只输出一个 JSON 对象，不要输出任何其他内容："
                '{"keywords": [3~6 个英文关键词，小写，覆盖需求的核心技术/领域，如 "crawler", "scraping", "spider"],'
                f'"languages": [相关编程语言名数组，取值只能来自 {lang_names}，'
                "不锁单个，拿不准就空],"
                '"topics": [相关主题词数组，小写 kebab-case，可空],'
                '"filters": {"created_within_days": 创建至今天数上限（正整数，可选）, "min_stars": 最新星数下限（正整数，可选）},'
                '"unsupported": [用户提到但无法检索的条件字符串数组，可选]}。'
                "filters 白名单只许 created_within_days 与 min_stars 两个字段，必须是正整数；"
                "白名单外条件（如'最近一周有提交'、'MIT 协议'）必须放入 unsupported，不得塞进 keywords。"
            )
            user = query
        else:
            system = (
                "你是 GitHub 项目雷达的搜索意图理解器。用户正在对上一轮搜索进行追问/修正。"
                "你会收到上一轮意图 JSON 和本轮新输入。请把两者合并成一个新意图（同一 JSON 契约），"
                "只输出一个 JSON 对象，不要输出任何其他内容："
                '{"keywords": [3~6 个英文关键词，小写，覆盖合并后需求的核心技术/领域],'
                f'"languages": [相关编程语言名数组，取值只能来自 {lang_names}，'
                "不锁单个，拿不准就空],"
                '"topics": [相关主题词数组，小写 kebab-case，可空],'
                '"filters": {"created_within_days": ..., "min_stars": ...},'
                '"unsupported": [...]}。'
                "合并规则：本轮输入是补充/修正——如'只要异步的'可追加 async 等关键词；"
                "如'换成 Go 的'可把语言替换为 Go 并保留相关关键词；如'不要 Python'则移除 Python；"
                "filters 同名字段由本轮新输入覆盖（如'放宽到 3 年内'），未提及的 filters/unsupported 保留；"
                "白名单外条件必须放入 unsupported，不得塞进 keywords。"
            )
            user = f"上一轮意图：{json.dumps(prev_intent, ensure_ascii=False)}\n本轮新输入：{query}"
        response_format = {"type": "json_object"}  # 协议层 JSON 约束（第一道防线）
        if thinking_cb is None:
            content = await self._interactive_chat(  # 低温度：解析求稳
                system=system,
                user=user,
                temperature=0.2,
                response_format=response_format,
            )
        else:
            content = await self._stream_chat(
                system=system, user=user, temperature=0.2, response_format=response_format, thinking_cb=thinking_cb
            )
        return _parse_suggested_topics(content)  # 复用 JSON 容错解析（剥 markdown 围栏 + 大括号截取）；None = 内容非法

    async def select_and_reason(
        self, *, intent_summary: str, candidates: list[dict], thinking_cb: Callable[[str], None] | None = None
    ) -> dict | None:
        """智能搜索精选（T-034→T-036，§17）：候选 50 → 至多 20 条 + 各一句中文推荐理由（编号引用防幻觉）。

        - candidates 为编号清单（id 从 1 起连续），每项含 full_name/description_en/language/stars；
        - 输出 JSON：{"results": [{"id": 候选编号, "reason": 中文理由}]}——只许从清单中按编号选择
          （LLM 无幻觉出不存在项目的机会），至多 20 条按匹配度排序，理由 ≤50 字"为什么适合你的意图"视角；
        - 返回 None = 响应解析不出合法 JSON（调用方退化粗排直出记 WARNING）；
          调用失败抛 DeepSeekError/DeepSeekAuthError（调用方处理：Auth 直通不可用，其余退化粗排）；
        - thinking_cb（T-041，§17.5）：缺省 None 走同步 ainvoke（协议参数与旧实现一致），非 None 走 astream
          流式形态；两形态同属交互路径——零自动重试，失败如实抛出或退化粗排（新版口径，见模块头）。
        """
        lines = [
            f"{i}. {c['full_name']} | 语言 {c['language'] or '未知'} | {c['stars']} 星"
            + (f" | {c['description_en']}" if c.get("description_en") else " | （无简介）")
            for i, c in enumerate(candidates, 1)
        ]
        system = (
            "你是技术雷达的编辑，为一位资深开发者读者从候选 GitHub 项目中挑选最匹配其搜索意图的项目。"
            "只输出一个 JSON 对象，不要输出任何其他内容："
            '{"results": [{"id": 候选编号, "reason": "中文推荐理由"}]}——'
            "从候选清单中按编号选择至多 20 个最匹配的项目，按匹配度从高到低排序；"
            "每个理由写一句 ≤50 字的中文，从“为什么适合你的意图”视角说明（不引用具体星数/排名数字）；"
            "没有匹配的就不选，不要凑数。"
        )
        user = f"用户搜索意图：{intent_summary}\n\n候选清单（只许从中选择）：\n" + "\n".join(lines)
        response_format = {"type": "json_object"}  # 协议层 JSON 约束（第一道防线）
        if thinking_cb is None:
            content = await self._interactive_chat(
                system=system,
                user=user,
                temperature=0.3,  # 与推荐语同值：允许一点措辞空间，但不许发散
                response_format=response_format,
            )
        else:
            content = await self._stream_chat(
                system=system, user=user, temperature=0.3, response_format=response_format, thinking_cb=thinking_cb
            )
        return _parse_suggested_topics(content)

    @staticmethod
    def _messages(system: str, user: str) -> list[dict[str, str]]:
        """LangChain 消息输入：system/user 两段（与旧实现请求体逐字段同构）。"""
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    @staticmethod
    def _bind_kwargs(temperature: float, response_format: dict | None) -> dict:
        """同步/批量调用点经 .bind() 下发的请求参数（决策 1）：temperature 恒下发；response_format 非 None
        才下发（纯文本方法请求体不出现该字段，与旧实现同姿态）；端点对该参数报 400 时走既有 4xx fail-fast
        链，不去参重发（拍板口径，生产网关已实测支持）。"""
        kwargs: dict = {"temperature": temperature}
        if response_format is not None:
            kwargs["response_format"] = response_format
        return kwargs

    @staticmethod
    def _stream_bind_kwargs(temperature: float, response_format: dict | None) -> dict:
        """流式调用点经 .bind() 下发的请求参数（决策 1 裁定 1，2026-10-14 主 agent 拍板）。

        response_format 经 `extra_body` 下发（非顶层 kwarg）：顶层形式会命中引擎的结构化输出解析路径
        （langchain-openai `_astream`：`if "response_format" in payload → beta.chat.completions.stream()`），
        逐 chunk 思维链被丢弃、只在流末聚合块里一次性透出（实证：spike result.json case2
        loc_chunk_counts reasoning_content=1；冒烟 thinking_increments=1、唯一增量到达于流末）。
        走 extra_body 后由 SDK 合并进请求体——**线上请求体不变**（仍带顶层 response_format，MockTransport
        断言锁死），引擎走普通流式路径，逐 chunk `additional_kwargs["reasoning_content"]` 恢复透出。
        """
        kwargs: dict = {"temperature": temperature}
        if response_format is not None:
            kwargs["extra_body"] = {"response_format": response_format}
        return kwargs

    async def _batch_once(self, params: dict) -> str:
        """with_retry 的重试单元（决策 2-4）：引擎最小调用面（含解析层异常归一）＋响应自检。"""
        try:
            message = await self._batch_model.bind(**params["bind"]).ainvoke(params["messages"])
        except _ENGINE_PARSE_ERRORS as exc:  # 引擎解析层拒收响应体 → 归"响应畸形"（F2-1）
            raise _malformed_engine_response(exc) from exc
        return _require_content(message)

    async def _batch_chat(
        self, *, system: str, user: str, temperature: float, response_format: dict | None = None
    ) -> str:
        """批量路径单次调用（决策 1/2）：with_retry 包住"引擎调用＋响应自检"。

        可重试类错误（超时/连接/5xx/空闲超时/响应畸形）按白名单零等待重试 self._max_retries 次，
        仍失败按六分支抛 DeepSeekError/DeepSeekAuthError（文案带"重试 N 次后放弃"）；429 与其余 4xx、
        账户类不重试（白名单外）。key 缺失在使用点拦住（与旧实现同姿态）。
        """
        self._require_api_key()
        params = {
            "bind": self._bind_kwargs(temperature, response_format),
            "messages": self._messages(system=system, user=user),
        }
        try:
            return await self._batch.ainvoke(params)
        except Exception as exc:
            self._raise_chat_error(exc, retried=True)

    async def _interactive_chat(
        self, *, system: str, user: str, temperature: float, response_format: dict | None = None
    ) -> str:
        """交互路径同步形态（决策 1/2）：零自动重试，失败即按六分支抛对外异常；响应自检同上。

        参数构造在 try 之外：解析层异常归一只施加于引擎调用这一最小面（F2-1 边界口径）。
        """
        self._require_api_key()
        runnable = self._interactive.bind(**self._bind_kwargs(temperature, response_format))
        messages = self._messages(system=system, user=user)
        try:
            message = await runnable.ainvoke(messages)
        except _ENGINE_PARSE_ERRORS as exc:  # 引擎解析层拒收响应体 → 归"响应畸形"（F2-1）
            raise _malformed_engine_response(exc) from exc
        except Exception as exc:
            self._raise_chat_error(exc, retried=False)
        return _require_content(message)

    async def _stream_chat(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        thinking_cb: Callable[[str], None],
        response_format: dict | None = None,
    ) -> str:
        """交互路径流式形态（T-041，§17.5；决策 2/3）：逐 chunk 喂思维链增量，content 增量累积后一次性
        自检返回（"只搬文本增量、末尾一次性解析"形态不变）。

        零自动重试（交互口径，流式与同步降级同姿态——首字节前失败也不重试，刻意行为变化）；思维链只认
        additional_kwargs["reasoning_content"]，缺/空零增量不报错；全程零 content 增量按旧文案如实抛出。
        response_format 经 `_stream_bind_kwargs`（extra_body）下发以保住逐段透出，线上请求体不变。
        解析层异常归一只包住"引擎流推进"这一步，调用方回调不在该面内（其异常走通用分类）。
        """
        self._require_api_key()
        runnable = self._interactive.bind(**self._stream_bind_kwargs(temperature, response_format))
        messages = self._messages(system=system, user=user)
        parts: list[str] = []
        try:
            iterator = runnable.astream(messages).__aiter__()
            while True:
                try:  # 引擎最小面（流式推进）
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                except _ENGINE_PARSE_ERRORS as exc:  # 引擎解析层拒收响应体 → 归"响应畸形"（F2-1）
                    raise _malformed_engine_response(exc) from exc
                think = _reasoning_delta(chunk)
                if think:
                    thinking_cb(think)
                piece = _chunk_text(chunk)
                if piece:
                    parts.append(piece)
        except Exception as exc:
            self._raise_chat_error(exc, retried=False)
        content = "".join(parts).strip()
        if not content:
            # F3-1：逐字恢复旧文案与旧异常类型（旧实现此分支抛 plain DeepSeekError）
            raise DeepSeekError("DeepSeek 流式响应畸形（未产生任何 content 增量）")
        return content

    def _require_api_key(self) -> None:
        """key 在使用点校验（与 GitHubClient 同姿态）：config 层不报错，这里第一刀拦住空 key。"""
        if not self._api_key:
            raise DeepSeekAuthError(
                "AI API key 为空：请在项目根 .env 配置 AI_API_KEY（或兼容键 DEEPSEEK_API_KEY），"
                "或注入同名系统环境变量；若置了 AI_ENABLED=0 则本服务不配真实 AI（本地开发口径）"
            )

    def _raise_chat_error(self, exc: Exception, *, retried: bool) -> NoReturn:
        """错误分类单点（决策 4 六分支映射）：抛 DeepSeekAuthError / DeepSeekError，人话文案逐字保留。

        分支与旧 `_handle_chat_error` 逐分支同序同文案（账户类 401/402/403 直通、内容审核 403 单条失败、
        5xx/超时/连接/响应畸形、其余 4xx 与未知异常不重试）；新增 StreamChunkTimeoutError（引擎空闲超时，
        TimeoutError 系，不在 openai 异常体系内）显式映射入超时分支。

        retried=True＝批量路径（with_retry 已按白名单重试 self._max_retries 次），5xx/超时/连接三类文案带
        "重试 N 次后放弃"；retried=False＝交互路径（零自动重试），同类文案不带重试声称——交互路径
        "重试 N 次后放弃"类文案消失属刻意行为变化（规格 ai-client／smart-search）。
        """
        if isinstance(exc, DeepSeekError):
            raise exc  # 外壳已构造好的对外异常（含 _ResponseMalformedError）：文案已定，原地抛出
        suffix = f"：重试 {self._max_retries} 次后放弃" if retried else ""
        if isinstance(exc, AuthenticationError):
            raise DeepSeekAuthError(
                "AI 账户类错误（HTTP 401）：请更新项目根 .env 中的 AI_API_KEY"
                "（或兼容键 DEEPSEEK_API_KEY）后重试；"
                f"响应：{exc.message[:200]}"
            ) from exc
        if isinstance(exc, PermissionDeniedError):
            if _is_content_policy_403(exc):
                raise DeepSeekError(
                    "AI 内容审核拦截（content_policy_violation），该仓跳过；"
                    f"响应：{exc.message[:200]}"
                ) from exc
            raise DeepSeekAuthError(
                "AI 账户类错误（HTTP 403）：请检查 AI 账户余额与权限；"
                f"响应：{exc.message[:200]}"
            ) from exc
        if isinstance(exc, APIStatusError):
            if exc.status_code == 402:
                raise DeepSeekAuthError(
                    "AI 账户类错误（HTTP 402）：请检查 AI 账户余额与权限；"
                    f"响应：{exc.message[:200]}"
                ) from exc
            if exc.status_code >= 500:
                raise DeepSeekError(f"DeepSeek 服务端错误（HTTP {exc.status_code}）{suffix}") from exc
            # 其他 4xx 不重试（含 429 限速：有意不重试——盲重试只会放大调用量，
            # 余额事故背景；未来若要支持应尊重 Retry-After 而非立即重试）
            raise DeepSeekError(f"DeepSeek 请求失败（HTTP {exc.status_code}）：{exc.message[:200]}") from exc
        if isinstance(exc, (APITimeoutError, APIConnectionError, StreamChunkTimeoutError)):
            raise DeepSeekError(f"DeepSeek 请求失败（{type(exc).__name__}: {exc}）{suffix}") from exc
        if isinstance(exc, APIError):
            # 引擎解析层透传的畸形响应（响应无法解析等），按"响应畸形"文案
            raise DeepSeekError(f"DeepSeek 响应畸形（{type(exc).__name__}: {exc}）") from exc
        # 未知异常不重试
        raise DeepSeekError(f"DeepSeek 请求失败（{type(exc).__name__}: {exc}）") from exc


@dataclass(frozen=True)
class _ListedItem:
    """上榜集元素：主榜行/新项目区行（row 携带 ReportRow）或新崛起区行（rising 携带 RisingRow）+ 所属分类榜名列表。

    T-018：新崛起区仓（周/季榜集内的 rising 行）row=None 而 rising 携带 RisingRow，主榜仓反之；
    推荐语生成按 rising 是否为空选择增量语境（主榜窗口增量 vs 在池增量）。
    T-033：新项目区行与主榜行同待遇（row 携带 ReportRow、窗口增量语境），仅"先上主榜还是先上新项目区"
    决定谁占位——同仓同榜不重影，listed 按 full_name 合并。
    """

    row: ReportRow | None
    categories: list[str]
    rising: RisingRow | None = None


def _load_repo_info(conn: sqlite3.Connection, full_names: list[str]) -> dict[str, sqlite3.Row]:
    """按 full_name 反查 id/description_en/description_zh/language（recommendations 外键与翻译/推荐语输入用）；
    分块防旧 SQLite 变量上限。"""
    info: dict[str, sqlite3.Row] = {}
    for offset in range(0, len(full_names), _NAME_LOOKUP_CHUNK):
        chunk = full_names[offset : offset + _NAME_LOOKUP_CHUNK]
        placeholders = ", ".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT id, full_name, description_en, description_zh, language FROM repos WHERE full_name IN "
            f"({placeholders})",
            chunk,
        ).fetchall()
        for row in rows:
            info[row["full_name"]] = row
    return info


def _scope_sets(
    conn: sqlite3.Connection, *, now: datetime
) -> tuple[dict[str, dict[str, _ListedItem]], list[str]]:
    """T-017/T-018/T-033 覆盖口径 S：三口径榜去重 ∪ 关注集（周/季 = 主榜 Top50 ∪ 新崛起区 Top10 ∪
    新项目区 Top20；total = 主榜 Top50 ∪ 新项目区 Top20，T-028 主榜 30→50）。

    返回 (listed_by_period, follow_names)：listed_by_period[period] = full_name → _ListedItem（榜单序保序）；
    follow_names 按 follows.created_at 序（页面关注序）。S 之外的仓库永远不译不生成（共识 §7 v4）。
    新崛起区行属上榜口径（决策 4 v2）：缺席仓进周/季榜集，总星榜集与关注集不受影响（total 无缺席概念恒空）；
    新项目区行属上榜口径（T-033 决策 5）：出席仓三口径都进榜集，与主榜行同待遇（row 非 None、窗口增量语境）。
    """
    topic_table = load_topics(TOPICS_PATH)
    as_of_iso = now.strftime(_ISO_FMT)
    listed_by_period: dict[str, dict[str, _ListedItem]] = {}
    for period in ("week", "quarter", "total"):
        boards = compute_boards(conn, topic_table, period=period, as_of=as_of_iso)
        listed: dict[str, _ListedItem] = {}
        for board in boards:
            for row in board.rows:
                item = listed.get(row.full_name)
                if item is None:
                    listed[row.full_name] = _ListedItem(row=row, categories=[board.label])
                else:
                    item.categories.append(board.label)  # 同一项目多榜出现：分类榜名累加（跨榜复用一条推荐语）
            # T-018：新崛起区行同属上榜口径（total 榜新区恒空自然不触发）
            for rising in board.rising_rows:
                item = listed.get(rising.full_name)
                if item is None:
                    listed[rising.full_name] = _ListedItem(row=None, categories=[board.label], rising=rising)
                else:
                    item.categories.append(board.label)
            # T-033：新项目区行同属上榜口径（与主榜行同待遇——row 非 None、窗口增量语境；total 榜同样有本区）
            for fresh in board.fresh:
                item = listed.get(fresh.full_name)
                if item is None:
                    listed[fresh.full_name] = _ListedItem(row=fresh, categories=[board.label])
                else:
                    item.categories.append(board.label)
        listed_by_period[period] = listed
    follow_names = [
        r["full_name"]
        for r in conn.execute(
            "SELECT r.full_name FROM follows f JOIN repos r ON r.id = f.repo_id ORDER BY f.created_at"
        )
    ]
    return listed_by_period, follow_names


class _ReadmeState:
    """README 拉取会话（T-017）：逐仓缓存 + 账户类错误后停拉降级（token 缺失/无效不再逐仓重试）。

    get() 对拉取失败/空一律返回 (None, None) 退化输入，绝不抛出阻塞；成功（拉到 sha）时
    stats["readme_fetched"] 计数（缓存命中不计）。
    """

    def __init__(
        self, github_client: GitHubClient | None, log: logging.Logger, max_chars: int = README_HEAD_CHARS
    ) -> None:
        self._client = github_client
        self._log = log
        self._max_chars = max_chars  # 截断入 prompt 上限（.env AI_README_HEAD_CHARS，单次调用成本主杠杆）
        self._auth_stopped = False
        self._cache: dict[str, tuple[str | None, str | None]] = {}

    async def get(self, full_name: str, stats: dict[str, int]) -> tuple[str | None, str | None]:
        """拉取并截断 README 正文与 blob sha；client 缺失/已停拉/失败/空 → (None, None) 退化。"""
        if self._client is None or self._auth_stopped:
            return None, None
        if full_name in self._cache:
            return self._cache[full_name]
        try:
            text, sha = await self._client.fetch_readme(full_name)
        except GitHubAuthError as exc:
            # token 缺失/无效是确定性配置错误：逐仓重试只会刷爆日志，停拉全量退化（任务书钉死口径）
            self._auth_stopped = True
            self._log.warning("GitHub README 拉取终止（账户类错误）：%s（后续推荐语退化元数据输入）", exc)
            return None, None
        except Exception as exc:
            self._log.warning("README 拉取失败，退化元数据输入 %s：%s", full_name, exc)
            return None, None
        if text is not None:
            text = text[: self._max_chars]  # 截断入 prompt（授权实施：取前部，README 惯例开头即定位）
        self._cache[full_name] = (text, sha)
        if sha:
            stats["readme_fetched"] += 1
        return text, sha


async def recommend_missing(
    conn: sqlite3.Connection,
    client: DeepSeekClient,
    *,
    now: datetime,
    log: logging.Logger | None = None,
    github_client: GitHubClient | None = None,
    refresh: bool = True,
    scope: tuple[dict[str, dict[str, _ListedItem]], list[str]] | None = None,
    on_progress: Callable[[dict[str, int]], None] | None = None,
) -> dict[str, int]:
    """三维度推荐语补缺（T-017，T-018 扩 S，T-033 再扩新项目区）＋ AI 概要补缺（T-024）：S = 三口径榜去重
    ∪ 关注集（周/季含新崛起区 Top10；三口径含新项目区 Top20，与主榜行同待遇）。

    - week：(repo_id, 'week', 当周标签) 缺失则生成（同周已存在跳过，幂等），输入带本周增量语境；
    - quarter：(repo_id, 'quarter', 当季标签) 缺失，或跑批日在半月窗口内且 generated_week ≠ 当周
      → 生成/REPLACE（每月 1 号、15 号重生；非窗口日跳过已有行）；
    - total：(repo_id, 'total', 'all') 缺失则生成（懒口径）；refresh=True（每日 ensure）且跑批日在半月窗口内
      时，才拉 README 比对 blob sha；sha 非空且变化 → REPLACE 重生并更新 sha（每月 1 号、15 号比对重生，
      非窗口日不拉 README 不比对，自愈）；总星文本不每周重刷、prompt 不引用数字；
    - summary（T-024，§11）：key=(repo_id, 'summary', 'all')，覆盖 S 全集（all_names 即上榜∪关注去重），
      仅 refresh=True（每日 ensure 路径）执行——§11 无手动入口，手动批量 worker refresh=False 整段跳过；
      缺失则生成；refresh 且窗口日才拉 README 比对 sha，sha 非空且与 cur['readme_sha'] 不同 → REPLACE 重生
      （与 total 段同一 F2-1 守卫：非窗口日不拉 README，本次 sha 未取到不触发重生、保留旧行旧指纹）；
      概要懒口径不每周重刷；
    - 输入含 README 正文（截断；拉取失败/空退化元数据，不持久化）；README 拉取失败绝不抛出阻塞
      （GitHub token 缺失/无效 → 全量退化、readme_sha 保持 NULL）；README 复用同一 _ReadmeState 实例
      （逐仓缓存，同轮同仓只拉一次，total 段与 summary 段共享）；
    - source='manual'（local-ai-relay 本地回填写入）的行为：quarter/total/summary 三处窗口重生守卫
      遇人工行直接跳过（不拉 README、不比 sha、不调用 AI、不覆盖）；缺失行照常补缺（写入 source='ai'），
      周榜每期次新行不受影响。手动单仓"生成/重新生成"（POST /api/recommend）属用户显式操作，不走本函数；
    - 单条失败记 WARNING 跳过计入统计，绝不抛出；DeepSeekAuthError（key 无效/余额/账户权限 401/402/403）
      直通整轮 handler；403 内容审核拦截（content_policy）属单仓失败，按 DeepSeekError 跳过该条；
    - 单条写入即 commit（崩溃不丢已花配额，重跑幂等补缺）。

    被每日 ensure_daily_ai 与手动批量补缺 worker（refresh=False，只补缺失不重生）共用。
    on_progress 在每条写库后回调 stats 快照（批量 worker 渐进更新进度用）。

    返回 {"listed", "recommended", "recommend_failed", "summarized", "summary_failed", "readme_fetched"}：
    listed＝S 去重仓库数；recommended/summarized＝推荐语/概要新写或覆盖条数；其余为各步成功/失败计数。
    手动批量 worker（refresh=False）只读 recommended/recommend_failed，不受概要两键影响。
    """
    log = log or logger
    stats = {"listed": 0, "recommended": 0, "recommend_failed": 0, "summarized": 0, "summary_failed": 0, "readme_fetched": 0}
    if scope is None:
        scope = _scope_sets(conn, now=now)
    listed_by_period, follow_names = scope
    all_names = list(follow_names)
    for period in ("week", "quarter", "total"):
        for full_name in listed_by_period[period]:
            if full_name not in all_names:
                all_names.append(full_name)
    stats["listed"] = len(all_names)
    if not all_names:
        return stats

    repo_info = _load_repo_info(conn, all_names)
    week_label = _week_label(now.date())
    quarter_label = _quarter_label(now.date())
    window_open = _refresh_window_open(now.date())  # 半月窗口：仅 1 号、15 号允许 quarter/total/summary 重生

    # 预取现有行（(repo_id, dimension, period_label) → 行），避免逐仓查询；手动 API 写入后本轮不重判
    # source 一并取：窗口重生守卫要按 'manual'（本地回填写入）跳过，不拉 README、不比 sha（local-ai-relay）
    existing: dict[tuple[int, str, str], sqlite3.Row] = {}
    for row in conn.execute(
        "SELECT repo_id, dimension, period_label, readme_sha, generated_week, source FROM recommendations"
    ):
        existing[(row["repo_id"], row["dimension"], row["period_label"])] = row

    readme_state = _ReadmeState(github_client, log, max_chars=get_settings().ai_readme_head_chars)

    # --- 周维度：缺 (repo_id, 'week', 当周标签) 则生成；同周已存在跳过（幂等，跨周标签不同自然生成新行） ---
    for full_name, item in listed_by_period["week"].items():
        info = repo_info.get(full_name)
        if info is None:
            continue
        key = (info["id"], "week", week_label)
        if key in existing:
            continue
        readme_text, _ = await readme_state.get(full_name, stats)
        # T-018：新区行带入池语境（在池增量/在池天数），主榜行保持既有增量参数
        if item.rising is not None:
            delta, stars, pool_days = item.rising.pool_delta, item.rising.stars, item.rising.pool_days
        else:
            assert item.row is not None  # _ListedItem 不变量：主榜仓 row 恒非 None（rising/row 互斥，评审 F3-5 加固）
            delta, stars, pool_days = (item.row.delta if item.row.delta is not None else 0), item.row.stars, None
        try:
            text = await client.recommend(
                full_name=full_name,
                description=info["description_zh"] or info["description_en"] or "（无简介）",
                language=info["language"] or "未知",
                delta=delta,
                stars=stars,
                categories=item.categories,
                dimension="week",
                pool_days=pool_days,
                readme=readme_text,
            )
        except DeepSeekAuthError:
            raise  # 同翻译段：账户类确定性错误直通
        except Exception as exc:
            log.warning("AI 推荐语生成失败，跳过 %s（week）：%s", full_name, exc)
            stats["recommend_failed"] += 1
            continue
        conn.execute(
            "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
            " VALUES (?, 'week', ?, ?, NULL, ?)",
            (info["id"], week_label, text, week_label),
        )
        conn.commit()
        stats["recommended"] += 1
        existing[key] = None  # 防御：同轮不重复判定
        if on_progress is not None:
            on_progress(stats)

    # --- 季维度：缺失，或跑批日在半月窗口内且 generated_week ≠ 当周 → 生成/REPLACE
    #     （每月 1 号、15 号重生；非窗口日跳过已有行；季榜 90 天窗口未满时自然为空，实现照做） ---
    for full_name, item in listed_by_period["quarter"].items():
        info = repo_info.get(full_name)
        if info is None:
            continue
        key = (info["id"], "quarter", quarter_label)
        cur = existing.get(key)
        # 已有行在非窗口日/手动批量（refresh=False）跳过；local-ai-relay：人工行（source='manual'）
        # 不受窗口重生影响——任何日期都不拉 README、不比 generated_week、不调用 AI、不覆盖
        if cur is not None and (
            not refresh or not window_open or cur["generated_week"] == week_label or cur["source"] == "manual"
        ):
            continue
        readme_text, _ = await readme_state.get(full_name, stats)
        # T-018：新区行带入池语境（在池增量/在池天数），主榜行保持既有增量参数
        if item.rising is not None:
            delta, stars, pool_days = item.rising.pool_delta, item.rising.stars, item.rising.pool_days
        else:
            assert item.row is not None  # _ListedItem 不变量：主榜仓 row 恒非 None（rising/row 互斥，评审 F3-5 加固）
            delta, stars, pool_days = (item.row.delta if item.row.delta is not None else 0), item.row.stars, None
        try:
            text = await client.recommend(
                full_name=full_name,
                description=info["description_zh"] or info["description_en"] or "（无简介）",
                language=info["language"] or "未知",
                delta=delta,
                stars=stars,
                categories=item.categories,
                dimension="quarter",
                pool_days=pool_days,
                readme=readme_text,
            )
        except DeepSeekAuthError:
            raise
        except Exception as exc:
            log.warning("AI 推荐语生成失败，跳过 %s（quarter）：%s", full_name, exc)
            stats["recommend_failed"] += 1
            continue
        conn.execute(
            "INSERT OR REPLACE INTO recommendations (repo_id, dimension, period_label, text, readme_sha,"
            " generated_week) VALUES (?, 'quarter', ?, ?, NULL, ?)",
            (info["id"], quarter_label, text, week_label),
        )
        conn.commit()
        stats["recommended"] += 1
        existing[key] = None
        if on_progress is not None:
            on_progress(stats)

    # --- 总星维度：S_total = total 榜 ∪ 关注集（关注未上榜仓生成总星文本，供关注页展示）；
    #     缺失则生成；refresh=True 且跑批日在半月窗口内时，才拉 README 比对 sha；sha 非空且变化
    #     → REPLACE 重生并更新 sha（非窗口日对已有行不拉 README 不比 sha，懒口径不每周重刷） ---
    total_names = list(follow_names)
    for full_name in listed_by_period["total"]:
        if full_name not in total_names:
            total_names.append(full_name)
    for full_name in total_names:
        info = repo_info.get(full_name)
        if info is None:
            continue
        rid = info["id"]
        key = (rid, "total", "all")
        cur = existing.get(key)
        # 非窗口日或手动批量（refresh=False）：已有行直接跳过，不拉 README、不比 sha（省 GitHub API）；
        # local-ai-relay：人工行（source='manual'）任何日期都跳过，不拉 README、不比 sha、不覆盖
        if cur is not None and (not refresh or not window_open or cur["source"] == "manual"):
            continue
        readme_text, sha = await readme_state.get(full_name, stats)
        # F2-1 修复：窗口日本次未拉到 sha（拉取失败/404/账户类停拉）不触发重生——保留旧行与旧指纹，
        # 防拉取失败制造每日 churn；README 被删除（404）的场景因此不再触发重生，属探测能力边界
        # （无 README 的仓 sha 恒 NULL，懒口径下不动）；sha 非空且变化才视为 README 变更
        if cur is not None and (sha is None or cur["readme_sha"] == sha):
            continue  # 已有且未触发重生：总星文本懒口径不重刷
        item = listed_by_period["total"].get(full_name)
        try:
            text = await client.recommend(
                full_name=full_name,
                description=info["description_zh"] or info["description_en"] or "（无简介）",
                language=info["language"] or "未知",
                delta=None,  # 总星维度无增量概念；prompt 不引用数字
                stars=None,
                categories=item.categories if item is not None else [],
                dimension="total",
                readme=readme_text,
            )
        except DeepSeekAuthError:
            raise
        except Exception as exc:
            log.warning("AI 推荐语生成失败，跳过 %s（total）：%s", full_name, exc)
            stats["recommend_failed"] += 1
            continue
        conn.execute(
            "INSERT OR REPLACE INTO recommendations (repo_id, dimension, period_label, text, readme_sha,"
            " generated_week) VALUES (?, 'total', 'all', ?, ?, ?)",
            (rid, text, sha, week_label),
        )
        conn.commit()
        stats["recommended"] += 1
        existing[key] = None
        if on_progress is not None:
            on_progress(stats)

    # --- 概要维度（T-024，§11）：key=(repo_id, 'summary', 'all')，覆盖 S 全集（all_names = 三榜去重 ∪ 关注）；
    #     仅 refresh=True（每日 ensure 路径）执行——§11 无手动入口，手动批量 worker refresh=False 整段跳过；
    #     缺失则生成；跑批日在半月窗口内才拉 README 比对 sha，sha 非空且与旧指纹不同 → REPLACE 重生
    #     （与 total 段同一 F2-1 守卫：非窗口日对已有行不拉 README，本次 sha 未取到不触发重生、保留旧行旧指纹）；
    #     概要与推荐语并存（文档视角，无维度概念，全页面同一条） ---
    if refresh:
        for full_name in all_names:
            info = repo_info.get(full_name)
            if info is None:
                continue
            rid = info["id"]
            key = (rid, "summary", "all")
            cur = existing.get(key)
            # 非窗口日：已有 summary 行直接跳过，不拉 README、不比 sha（省 GitHub API）；
            # local-ai-relay：人工行（source='manual'）任何日期都跳过，不拉 README、不比 sha、不覆盖
            if cur is not None and (not window_open or cur["source"] == "manual"):
                continue
            readme_text, sha = await readme_state.get(full_name, stats)  # 复用同一 _ReadmeState：同轮同仓缓存命中
            if cur is not None and (sha is None or cur["readme_sha"] == sha):
                continue  # 已有且未触发重生：概要懒口径不重刷（README 变更窗口日才重生）
            try:
                text = await client.summarize(
                    full_name=full_name,
                    description=info["description_zh"] or info["description_en"] or "（无简介）",
                    language=info["language"] or "未知",
                    readme=readme_text,
                )
            except DeepSeekAuthError:
                raise  # 账户类确定性错误直通（同推荐语段）
            except Exception as exc:
                log.warning("AI 概要生成失败，跳过 %s：%s", full_name, exc)
                stats["summary_failed"] += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO recommendations (repo_id, dimension, period_label, text, readme_sha,"
                " generated_week) VALUES (?, 'summary', 'all', ?, ?, ?)",
                (rid, text, sha, week_label),
            )
            conn.commit()
            stats["summarized"] += 1
            existing[key] = None  # 防御：同轮不重复判定
            if on_progress is not None:
                on_progress(stats)

    return stats


async def ensure_daily_ai(
    conn: sqlite3.Connection,
    client: DeepSeekClient,
    *,
    now: datetime,
    log: logging.Logger | None = None,
    github_client: GitHubClient | None = None,
) -> dict[str, int]:
    """每日 AI 生成（T-017 重写，原名 ensure_weekly_ai）：范围集翻译收窄＋三维度推荐语补缺/刷新＋AI 概要（T-024）。

    步骤：a) 计算范围集 S = 三口径榜去重 ∪ 关注集（周/季 = 主榜 Top50 ∪ 新崛起区 Top10 ∪ 新项目区 Top20，
    T-018；T-028 主榜 30→50；T-033 加入新项目区）；
    b) 翻译段收窄：只译 S 内 description_zh IS NULL 且英文非空无 CJK 的仓（原文变更采集层已清译文，
    当日本轮自然重译；译过的不重译；S 之外永不翻译——v3 全池口径作废）；
    c) 推荐语三维度＋概要（口径详见 recommend_missing docstring；refresh=True → 概要段随行执行）；
    d) 单条失败记 WARNING 跳过计入统计，绝不抛出；DeepSeekAuthError（key 无效/余额/账户权限）
    是确定性配置错误，直通抛出由调用方整轮捕获（与采集层 GitHubAuthError 同姿态）；
    403 内容审核拦截（content_policy）属单仓失败，按 DeepSeekError 跳过该条；
    e) key 未配置记 INFO 直接返回零统计；GitHub token 缺失/无效 → README 全量退化不报错。

    事务选择：单条写入即 commit（不开整体事务）——后台串行、量级小（S ≤ 数百仓），
    崩溃时已完成写入不丢（不浪费已花的 API 配额），重跑靠"已译/同维度同期已存在"幂等跳过自然补缺。

    返回 {"listed", "translated", "translate_failed", "recommended", "recommend_failed",
    "summarized", "summary_failed", "readme_fetched"}：listed＝S 去重仓库数，其余为各步成功/失败计数。
    """
    log = log or logger
    stats = {
        "listed": 0,
        "translated": 0,
        "translate_failed": 0,
        "recommended": 0,
        "recommend_failed": 0,
        "summarized": 0,
        "summary_failed": 0,
        "readme_fetched": 0,
    }
    if not get_settings().ai_api_key:
        log.info("AI 未配置或已禁用（AI_API_KEY/AI_ENABLED）：跳过 AI 翻译与推荐语生成（降级，榜单服务照常）")
        return stats

    # a) 范围集 S（三口径榜一次算齐，翻译段与推荐段共用，避免重复计算）
    scope = _scope_sets(conn, now=now)
    listed_by_period, follow_names = scope
    all_names = list(follow_names)
    for period in ("week", "quarter", "total"):
        for full_name in listed_by_period[period]:
            if full_name not in all_names:
                all_names.append(full_name)
    stats["listed"] = len(all_names)
    if not all_names:
        return stats

    # b) 翻译段（T-017 收窄：只译 S 内未译；手动单个翻译 API 不受此限，任何池内仓可手动触发）
    repo_info = _load_repo_info(conn, all_names)
    for full_name in all_names:
        info = repo_info.get(full_name)
        if info is None or info["description_zh"] is not None:
            continue
        text_en = info["description_en"]
        if not text_en or not text_en.strip():
            continue  # GitHub 官方允许无简介：空描述跳过
        if has_cjk(text_en):
            continue  # 原文已含中文（含中英混排），不重复译
        try:
            zh = await client.translate(text_en)
        except DeepSeekAuthError:
            raise  # 账户类确定性错误（401/402/403 账户权限）：逐条重试只会刷爆日志，直通整轮 handler
        except Exception as exc:
            log.warning("AI 翻译失败，跳过 %s：%s", full_name, exc)
            stats["translate_failed"] += 1
            continue
        # 低-6 护栏：写库前不重查现值的竞态防护（并发单个翻译 API 已写入则本条跳过不计）
        cur = conn.execute(
            "UPDATE repos SET description_zh = ? WHERE id = ? AND description_zh IS NULL AND description_en = ?",
            (zh, info["id"], text_en),
        )
        conn.commit()
        if cur.rowcount:
            stats["translated"] += 1

    # c) 推荐语三维度＋概要（refresh=True：quarter/total/summary 均在半月窗口日才重生；概要段仅 refresh 路径）
    sub = await recommend_missing(
        conn, client, now=now, log=log, github_client=github_client, refresh=True, scope=scope
    )
    stats["recommended"] += sub["recommended"]
    stats["recommend_failed"] += sub["recommend_failed"]
    stats["summarized"] += sub["summarized"]
    stats["summary_failed"] += sub["summary_failed"]
    stats["readme_fetched"] += sub["readme_fetched"]

    log.info(
        "AI 每日生成汇总（%s）：覆盖 %d、新译 %d（失败 %d）、新推荐 %d（失败 %d）、概要 %d（失败 %d）、README 拉取 %d",
        _week_label(now.date()),
        stats["listed"],
        stats["translated"],
        stats["translate_failed"],
        stats["recommended"],
        stats["recommend_failed"],
        stats["summarized"],
        stats["summary_failed"],
        stats["readme_fetched"],
    )
    return stats
