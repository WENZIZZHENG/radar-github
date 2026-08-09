"""AI 服务（T-011）：DeepSeek 懒翻译＋周榜推荐理由生成＋全程降级（《架构决策记录》决策 5/6）。

口径（任务书钉死，勿自由发挥）：
- 懒翻译（决策 5）：只对当周上榜集（17 张周榜 Top30 去重）且 description_zh IS NULL、英文简介非空、
  不含 CJK 的仓库逐条调翻译回填 repos.description_zh；未上榜项目一律不动——AI 成本只花在看得见的地方；
- 推荐理由（决策 6）：对上榜集逐仓生成 2~3 句中文推荐语（按项目生成一次、跨榜复用），
  按 (repo_id, ISO 周标签) 写入 recommendations；同周已存在跳过（幂等），跨周自然生成新行——
  "每周重新生成"靠周标签区分实现，不覆盖历史周；
- 降级：DEEPSEEK_API_KEY 未配置 → 记 INFO 返回零统计，服务照常；单条 translate/recommend 失败 →
  记 WARNING 跳过该条计入统计，绝不抛出；AI 整段异常由调用方（daily_job）吞掉记 ERROR——
  任何情况下快照主流程不受影响。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from app.classify import load_topics
from app.config import BASE_DIR, get_settings
from app.report import ReportRow, compute_boards

CHAT_URL = "https://api.deepseek.com/v1/chat/completions"
CHAT_MODEL = "deepseek-chat"
TOPICS_PATH = BASE_DIR / "config" / "topics.yaml"  # 与 web 层同一路径来源（app/web/routes.py TOPICS_PATH）

DEFAULT_MAX_RETRIES = 1  # 任务书口径：重试一次后仍失败 → 抛清晰异常
RETRY_WAIT_SECONDS = 1.0  # 仅重试一次，固定等 1 秒即可，不引指数退避
REQUEST_TIMEOUT_SECONDS = 60.0  # LLM 响应慢于普通 REST，放宽到 60 秒

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"  # schema 硬约定：UTC 定长（与 app.report 同口径）

# CJK 统一表意文字命中即视为已有中文（含中英混排简介）：原文已有中文不再送译
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 按 full_name 反查 repo_id 的分块大小：防御旧编译 SQLite 的 999 变量上限（与 app.report 同口径）
_NAME_LOOKUP_CHUNK = 500

logger = logging.getLogger(__name__)


class DeepSeekError(RuntimeError):
    """DeepSeek 调用失败的基类：单条失败由 ensure_weekly_ai 捕获跳过，不阻断整轮。"""


class DeepSeekAuthError(DeepSeekError):
    """API key 缺失/无效及账户类确定性错误（401 key 无效、402 余额不足、403 无权限）：逐条重试无意义，直通整轮 handler（与 GitHubAuthError 同姿态）。"""


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


def _extract_content(response: httpx.Response) -> str | None:
    """从 chat/completions 响应取首条 message.content 并去首尾空白；JSON 畸形/结构缺失/空内容一律 None。"""
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str):
        return None
    return content.strip() or None


class DeepSeekClient:
    """httpx AsyncClient 封装 DeepSeek chat/completions；transport / sleep 可注入（单测 MockTransport 离线跑）。"""

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._api_key = api_key
        self._sleep = sleep
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> DeepSeekClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def translate(self, text: str) -> str:
        """英文简介 → 中文译文本体（一条一次；prompt 钉死只输出译文：无引号包裹、无"翻译："前缀）。"""
        return await self._chat(
            system=(
                "你是开源项目简介翻译器。把用户给出的 GitHub 仓库英文简介翻译成自然简洁的中文，"
                "只输出译文本体：不要加引号包裹，不要加“翻译：”等任何前缀，不要解释。"
                "项目名、品牌名、技术专有名词保留原文。"
            ),
            user=text,
            temperature=0.2,  # 低温度：翻译求稳不求花
        )

    async def recommend(
        self, *, full_name: str, description: str, language: str, delta: int, stars: int, categories: list[str]
    ) -> str:
        """上榜推荐理由：2~3 句中文推荐语本体（prompt 钉死无引号包裹、无"推荐理由："前缀、不用列表）。"""
        user = (
            f"仓库：{full_name}\n"
            f"简介：{description}\n"
            f"主语言：{language}\n"
            f"本周新增星数：{delta}\n"
            f"总星数：{stars}\n"
            f"上榜分类：{'、'.join(categories)}"
        )
        return await self._chat(
            system=(
                "你是技术雷达的编辑，为一位资深开发者读者写 GitHub 周榜上榜项目的推荐理由。"
                "根据给出的仓库信息写 2~3 句中文推荐语：第一句说清项目是做什么的，"
                "其余说明为什么本周值得关注（结合本周增星、总星数与上榜分类），有选型参考价值时点明。"
                "只输出推荐语本体：不要加引号包裹，不要加“推荐理由：”等前缀，不要用列表或标题。"
            ),
            user=user,
            temperature=0.3,  # 低温度：推荐语允许一点措辞空间，但不许发散
        )

    async def _chat(self, *, system: str, user: str, temperature: float) -> str:
        """一次 chat/completions 调用：超时/传输错误/5xx/响应畸形重试一次后仍失败 → 抛清晰异常。"""
        # key 在使用点校验（与 GitHubClient 同姿态）：config 层不报错，这里第一刀拦住空 key
        if not self._api_key:
            raise DeepSeekAuthError(
                "DeepSeek API key 为空：请在项目根 .env 配置 DEEPSEEK_API_KEY，或注入同名系统环境变量"
            )
        payload = {
            "model": CHAT_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(CHAT_URL, json=payload)
            except httpx.HTTPError as exc:  # 超时/连接错误等传输层失败
                if attempt >= self._max_retries:
                    raise DeepSeekError(
                        f"DeepSeek 请求失败（{type(exc).__name__}: {exc}）：重试 {self._max_retries} 次后放弃"
                    ) from exc
                await self._sleep(RETRY_WAIT_SECONDS)
                continue
            status = response.status_code
            if status in (401, 402, 403):
                # 账户类确定性错误（401 key 无效 / 402 余额不足 / 403 无权限）：逐条重试无意义只会刷爆
                # 日志且次日重演，与空 key 同姿态直通整轮 handler（评审低-3：401 之外的账户类 4xx 并入）
                hint = "请更新项目根 .env 中的 DEEPSEEK_API_KEY 后重试" if status == 401 else "请检查 DeepSeek 账户余额与权限"
                raise DeepSeekAuthError(f"DeepSeek 账户类错误（HTTP {status}）：{hint}；响应：{response.text[:200]}")
            if status >= 500:
                if attempt >= self._max_retries:
                    raise DeepSeekError(f"DeepSeek 服务端错误（HTTP {status}）：重试 {self._max_retries} 次后放弃")
                await self._sleep(RETRY_WAIT_SECONDS)
                continue
            if status >= 400:
                raise DeepSeekError(f"DeepSeek 请求失败（HTTP {status}）：{response.text[:200]}")
            content = _extract_content(response)
            if content is None:
                if attempt >= self._max_retries:
                    raise DeepSeekError(f"DeepSeek 响应畸形（JSON 无法解析或缺 content）：{response.text[:200]}")
                await self._sleep(RETRY_WAIT_SECONDS)
                continue
            return content
        raise DeepSeekError("不可达：重试循环异常退出")  # 防御：max_retries≥0 时循环至少执行一次


@dataclass(frozen=True)
class _ListedItem:
    """上榜集元素：榜单行 + 所属分类榜名列表（语言榜 label 与命中主题榜 label，可多榜重复）。"""

    row: ReportRow
    categories: list[str]


def _load_repo_info(conn: sqlite3.Connection, full_names: list[str]) -> dict[str, sqlite3.Row]:
    """按 full_name 反查 id/description_zh（recommendations 外键与已译判定用）；分块防旧 SQLite 变量上限。"""
    info: dict[str, sqlite3.Row] = {}
    for offset in range(0, len(full_names), _NAME_LOOKUP_CHUNK):
        chunk = full_names[offset : offset + _NAME_LOOKUP_CHUNK]
        placeholders = ", ".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, full_name, description_zh FROM repos WHERE full_name IN ({placeholders})", chunk
        ).fetchall()
        for row in rows:
            info[row["full_name"]] = row
    return info


async def ensure_weekly_ai(
    conn: sqlite3.Connection,
    client: DeepSeekClient,
    *,
    now: datetime,
    log: logging.Logger | None = None,
) -> dict[str, int]:
    """当周上榜集的懒翻译＋推荐理由生成（幂等，可断点续跑），返回统计 dict。

    步骤：load_topics → compute_boards(period="week", as_of=now) 算 17 榜 → 去重上榜集 S →
    a) S 中未译且英文非空无 CJK 的逐条翻译回填 repos.description_zh；
    b) S 中缺 (repo_id, 当周 ISO 周标签) 的逐条生成推荐语 INSERT recommendations；
    c) 单条失败记 WARNING 跳过计入统计，绝不抛出；DeepSeekAuthError（key 无效）是确定性配置错误，
       逐条重试无意义，直通抛出由调用方整轮捕获（与采集层 GitHubAuthError 同姿态）；
    d) key 未配置记 INFO 直接返回零统计。

    事务选择：单条写入即 commit（不开整体事务）——后台串行、量级小（≤510 条），
    崩溃时已完成写入不丢（不浪费已花的 API 配额），重跑靠"已译/同周已存在"幂等跳过自然补缺；
    代价是极端情况下两表进度不一致（译了没推荐），次日重跑即收敛，可接受。

    返回 {"listed", "translated", "translate_failed", "recommended", "recommend_failed"}：
    listed＝上榜集去重仓库数，其余为各步成功/失败计数。
    """
    log = log or logger
    stats = {"listed": 0, "translated": 0, "translate_failed": 0, "recommended": 0, "recommend_failed": 0}
    if not get_settings().deepseek_api_key:
        log.info("DEEPSEEK_API_KEY 未配置：跳过本周 AI 翻译与推荐语生成（降级，榜单服务照常）")
        return stats

    topic_table = load_topics(TOPICS_PATH)
    boards = compute_boards(conn, topic_table, period="week", as_of=now.strftime(_ISO_FMT), top_n=30)
    listed: dict[str, _ListedItem] = {}  # full_name → 行信息＋分类榜名；dict 保序，结果可复现
    for board in boards:
        for row in board.rows:
            item = listed.get(row.full_name)
            if item is None:
                listed[row.full_name] = _ListedItem(row=row, categories=[board.label])
            else:
                item.categories.append(board.label)  # 同一项目多榜出现：分类榜名累加（决策 6 跨榜复用一条推荐语）
    stats["listed"] = len(listed)
    if not listed:
        return stats

    week = _week_label(now.date())
    repo_info = _load_repo_info(conn, list(listed))

    # a) 懒翻译：已译/中文/空描述一律跳过
    for full_name, item in listed.items():
        info = repo_info.get(full_name)
        if info is None or info["description_zh"] is not None:
            continue  # 防御：榜上仓库必在 repos（compute_boards 同源），查不到跳过；已译跳过
        text_en = item.row.description_en
        if not text_en or not text_en.strip():
            continue  # GitHub 官方允许无简介：空描述跳过
        if has_cjk(text_en):
            continue  # 原文已含中文（含中英混排），不重复译
        try:
            zh = await client.translate(text_en)
        except DeepSeekAuthError:
            raise  # 账户类确定性错误（401/402/403）：逐条重试只会刷爆日志，直通整轮 handler
        except Exception as exc:
            log.warning("AI 翻译失败，跳过 %s：%s", full_name, exc)
            stats["translate_failed"] += 1
            continue
        conn.execute("UPDATE repos SET description_zh = ? WHERE full_name = ?", (zh, full_name))
        conn.commit()
        stats["translated"] += 1

    # b) 推荐理由：同周已存在跳过（幂等）；跨周周标签不同自然生成新行
    repo_info = _load_repo_info(conn, list(listed))  # 重查一次：让本轮刚译好的 description_zh 进推荐语输入
    done_ids = {
        row["repo_id"] for row in conn.execute("SELECT repo_id FROM recommendations WHERE report_week = ?", (week,))
    }
    for full_name, item in listed.items():
        info = repo_info.get(full_name)
        if info is None or info["id"] in done_ids:
            continue
        row = item.row
        try:
            text = await client.recommend(
                full_name=full_name,
                description=info["description_zh"] or row.description_en or "（无简介）",  # 优先中文（含本轮刚译的）
                language=row.language or "未知",
                delta=row.delta if row.delta is not None else 0,  # 周榜出席行 delta 恒非 None；防御兜底
                stars=row.stars,
                categories=item.categories,
            )
        except DeepSeekAuthError:
            raise  # 同翻译段：账户类确定性错误直通
        except Exception as exc:
            log.warning("AI 推荐语生成失败，跳过 %s：%s", full_name, exc)
            stats["recommend_failed"] += 1
            continue
        conn.execute(
            "INSERT INTO recommendations (repo_id, report_week, text) VALUES (?, ?, ?)",
            (info["id"], week, text),
        )
        conn.commit()
        stats["recommended"] += 1

    log.info(
        "AI 周度生成汇总（%s）：上榜 %d、新译 %d（失败 %d）、新推荐 %d（失败 %d）",
        week,
        stats["listed"],
        stats["translated"],
        stats["translate_failed"],
        stats["recommended"],
        stats["recommend_failed"],
    )
    return stats
