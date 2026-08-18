"""T-008 榜单页面（SSR）＋ T-009 关注 API ＋ T-015 关注独立页 P6 ＋ T-010 标签（增删 API＋P5 筛选页）：
本周报告（含历史周次切换）/ 季度回顾 / 总星榜 / 我的关注 / 关注写端点 / 标签写端点与筛选页。

职责边界（任务书钉死，越界打回）：
- 榜单口径全部委托 app.report.compute_boards / compute_repo_deltas（《架构决策记录》决策 3/4 唯一实现），本层不重算；
- 本层只做：URL 期次参数 → as_of 换算、首期空态降级（《交互流程说明》§4）、展示格式化（数字/颜色/锚点）；
- P6 我的关注（v1.3 独立页）：增量复用 compute_repo_deltas 关注集过滤，本层只做分组与组内/组间排序；
- 关注写操作（T-009）：三态分流与动态入池在 app.follows / app.collector.snapshot，本层只做输入校验与 HTTP 状态码映射
  （v1.3 起响应不再带 card_html——P1 关注区已移出，无可插入区域）；
- 标签写操作与 P5 筛选页（T-010）：tags 表增删在本层（幂等/校验/404 口径见 /api/tags 路由），行内交互在 radar.js；
- 翻译写端点（T-016）：单个强制重译 / 批量只补 NULL（见 /api/translate、/api/translate-missing，
  《交互流程说明》§7.4 钉死口径；不触碰 recommendations 表）；
- 推荐语写端点（T-017）：单个强制重生 / 批量只补缺失（见 /api/recommend、/api/recommend-missing，
  《交互流程说明》§8.4 钉死口径；不触碰 repos 翻译字段）；
- 手动同步端点（T-029）：POST /api/sync 后台起任务／GET /api/sync/status 轮询（《交互流程说明》§14；
  同步＝daily_job 全链路，运行锁与状态在 app.jobs、与调度器共用一把锁，本层只判返回值不重实现链路）；
- 本层对 recommendations 表展示映射按 (dimension, period_label) 取（§8.1：周页→周文本、季页→季文本、
  总星/关注/标签页→总星文本），缺则该行无推荐语块（AI 降级形态）；
- AI 概要（T-024，§11）：展示映射固定取 dimension='summary' 且 period_label='all'（文档视角、无维度概念、
  全页面同一条，S 全集覆盖），缺则该行无概要块（AI 降级形态，不留空框）；无任何手动生成入口。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.ai import (
    DeepSeekAuthError,
    DeepSeekClient,
    _ReadmeState,
    _scope_sets,
    has_cjk,
    recommend_missing,
)
from app.candidates import NOT_RECOMMENDED, POOL_COUNT_THRESHOLD
from app.classify import LANGUAGES, OTHER_LANGUAGE_KEY, classify_language, load_topics
from app.collector.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubNotFoundError,
)
from app.config import BASE_DIR, get_settings
from app.db import get_conn
from app.follows import follow_repo, unfollow_repo
from app.jobs import sync_status, try_start_sync
from app.report import (
    OTHER_TOPIC_KEY,
    Board,
    ReportRow,
    RisingRow,
    _created_year,
    compute_boards,
    compute_repo_deltas,
    load_board_cache,
    pool_days_label,
    quarter_label,
    week_label,
)

_WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = _WEB_DIR / "static"  # 供 app.main 挂载 StaticFiles（/static）
TOPICS_PATH = BASE_DIR / "config" / "topics.yaml"

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=_WEB_DIR / "templates")
router = APIRouter()

# 标签 chip 链接的 path 段编码（quote safe=""：标签可含 / 与空格，全量百分号编码；Jinja 内置 urlencode
# 是 quote_plus（空格 → +），FastAPI 路径参数用 unquote 解码不会还原 +，故自备过滤器）
templates.env.filters["quote_tag"] = lambda s: quote(s, safe="")

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"  # schema 硬约定：UTC 定长（与 app.report 同口径）
_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")

# 增量口径名义窗口天数（与 app.report._INCREMENT_WINDOWS 同值）：元信息行窗口文案、行级实际窗口标注用
_NOMINAL_DAYS = {"week": 7, "quarter": 90}

# 语言徽标点色：照搬示意图 v3（GitHub Linguist 色针对深底微调；仅装饰用，语义由文字承载）
LANG_COLORS = {
    "JavaScript": "#f1e05a",
    "TypeScript": "#5ca9e6",
    "Python": "#4b8bbe",
    "Go": "#00ADD8",
    "Rust": "#dea584",
    "Java": "#c9842a",
    "C++": "#f34b7d",
    "C": "#9aa7b8",
    "CSS": "#a074c4",
    "Ruby": "#e15b4c",
    "Zig": "#ec915c",
    "Scala": "#d05a4d",
}
_DEFAULT_LANG_COLOR = "#8b98a9"

# 语言 key → GitHub 精确名（LANGUAGES 的反向映射）：P6 分组块头展示用；"其它语言"组单独给中文名
_LANG_KEY_TO_NAME = {v: k for k, v in LANGUAGES.items()}

_topic_table_cache: dict | None = None


def _topic_table() -> dict:
    """词表模块级缓存：词表是封板物、运行期不变，改动需重启进程生效（S 档可接受，避免每请求读盘）。"""
    global _topic_table_cache
    if _topic_table_cache is None:
        _topic_table_cache = load_topics(TOPICS_PATH)
    return _topic_table_cache


# ===== 期次参数 → as_of 换算 =====


def _week_label(d: date) -> str:
    """date → ISO 周标签。T-027 起换算单一事实源迁至 app.report.week_label（precompute_boards 落表
    label 与页面期次换算共用），本层保留同名私有别名，调用点零改动；格式口径见 report.week_label。"""
    return week_label(d)


def _quarter_label(d: date) -> str:
    """date → 季标签。T-027 别名同 _week_label，见 report.quarter_label。"""
    return quarter_label(d)


def _parse_week_param(raw: str) -> date:
    """"2026-W32" → 该周周一 date。非法输入直接 400：期次是用户可见 URL，静默回退会让错误链接看起来正常。"""
    if not _WEEK_RE.fullmatch(raw):
        raise HTTPException(status_code=400, detail=f"周次格式应为 ISO 周（如 2026-W32），收到：{raw!r}")
    try:
        monday = datetime.strptime(f"{raw}-1", "%G-W%V-%u").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"不存在的 ISO 周次：{raw!r}") from None
    if _week_label(monday) != raw:
        # 防御：strptime 对越界周数（如非闰周的 W53）不保证报错，回算标签不一致即拒绝
        raise HTTPException(status_code=400, detail=f"不存在的 ISO 周次：{raw!r}")
    return monday


def _parse_quarter_param(raw: str) -> tuple[int, int]:
    """"2026-Q3" → (2026, 3)；非法输入同周次口径直接 400。"""
    m = _QUARTER_RE.fullmatch(raw)
    if not m:
        raise HTTPException(status_code=400, detail=f"季度格式应为 2026-Q3，收到：{raw!r}")
    return int(m.group(1)), int(m.group(2))


def _quarter_end_date(year: int, q: int) -> date:
    """季度最后一天：次月首日减一天（Q4 跨年）。"""
    if q == 4:
        return date(year, 12, 31)
    return date(year, q * 3 + 1, 1) - timedelta(days=1)


def _resolve_week(week: str | None, now: datetime) -> tuple[str, str, date]:
    """周次参数 → (周标签, as_of ISO, as_of 日期)。

    as_of 口径（任务书钉死）：历史周取该周周日 23:59:59Z；当前周取 now——不预支未来，
    否则当前周榜单会随"周日内剩余时间"出现无法解释的口径漂移。
    """
    cur = _week_label(now.date())
    if week is None:
        return cur, now.strftime(_ISO_FMT), now.date()
    monday = _parse_week_param(week)
    label = _week_label(monday)
    if label > cur:
        raise HTTPException(status_code=400, detail=f"周次 {label} 尚未到来（当前 {cur}）")
    if label == cur:
        return label, now.strftime(_ISO_FMT), now.date()
    sunday = monday + timedelta(days=6)
    return label, f"{sunday.isoformat()}T23:59:59Z", sunday


def _resolve_quarter(quarter: str | None, now: datetime) -> tuple[str, str, date]:
    """季度参数 → (季度标签, as_of ISO, as_of 日期)：口径同周次（历史季取季末 23:59:59Z，当前季取 now）。"""
    cur = _quarter_label(now.date())
    if quarter is None:
        return cur, now.strftime(_ISO_FMT), now.date()
    year, q = _parse_quarter_param(quarter)
    label = f"{year}-Q{q}"
    if label > cur:
        raise HTTPException(status_code=400, detail=f"季度 {label} 尚未到来（当前 {cur}）")
    if label == cur:
        return label, now.strftime(_ISO_FMT), now.date()
    end = _quarter_end_date(year, q)
    return label, f"{end.isoformat()}T23:59:59Z", end


def _earliest_labels(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """最早可回看期次（周/季）：取跟踪池最早入池日——比 MIN(captured_at) 便宜且不碰 star_snapshots 全索引扫。"""
    raw = conn.execute("SELECT MIN(created_at) FROM repos").fetchone()[0]
    if raw is None:
        return None
    d = date.fromisoformat(raw[:10])  # created_at 定长 ISO，前 10 位即日期
    return _week_label(d), _quarter_label(d)


def _switcher(period: str, label: str, now: datetime, earliest_label: str) -> dict:
    """期次分段控件（示意图顶栏右侧）：上一期/下一期，越界禁用（禁用原因走 tooltip——流程说明 §0）。

    季度回看用与周次同款分段控件而非《交互流程说明》§1 的"期次下拉"：当前仅单个季度可选，
    下拉无内容可列；控件形态已在 v3 示意图冻结，保持一致（流程偏差已在交付报告留痕）。
    """
    cur = _week_label(now.date()) if period == "week" else _quarter_label(now.date())
    if period == "week":
        monday = _parse_week_param(label)  # label 上游已校验，这里复用解析拿周一做 ±7 天
        prev_url = f"/?week={_week_label(monday - timedelta(days=7))}"
        next_url = f"/?week={_week_label(monday + timedelta(days=7))}"
        unit = "周"
    else:
        year, q = _parse_quarter_param(label)
        py, pq = (year - 1, 4) if q == 1 else (year, q - 1)
        ny, nq = (year + 1, 1) if q == 4 else (year, q + 1)
        prev_url = f"/quarter?quarter={py}-Q{pq}"
        next_url = f"/quarter?quarter={ny}-Q{nq}"
        unit = "季"
    return {
        "current": label,
        "prev_text": f"‹ 上一{unit}",
        "prev_url": prev_url if label > earliest_label else None,  # None → 禁用"已是最早一期"
        "next_text": f"下一{unit} ›",
        "next_url": next_url if label < cur else None,  # None → 禁用"已是最新一期"
    }


# ===== 展示格式化 =====


def _fmt_stars(value: int) -> str:
    """总星紧凑格式（示意图口径）：<1000 原样，>=1000 一位小数 k（428.3k 也不换 m）。"""
    if value < 1000:
        return str(value)
    return f"{value / 1000:.1f}k"


def _fmt_delta(delta: int) -> str:
    """增量带正号千分位：1847 → +1,847；负数格式说明自带负号：-50。"""
    return f"{delta:+,}"


def _endpoint_note(captured_at: str | None) -> str | None:
    """端点快照日期标注（T-020，流程说明 §10）：定长 ISO 前 10 字符直接切片（schema 硬约定，不做日期解析）。

    四个行视图装配点共用的展示字段，None → 模板不渲染（防御行）。"""
    if captured_at is None:
        return None
    return f"端点快照 {captured_at[:10]}"


def _meta(conn: sqlite3.Connection, period: str, as_of_date: date) -> dict:
    """元信息行（流程说明 §2 第 1 层，T-028 消歧）：统计窗口、数据截至（最新快照的北京日期）、页面渲染时刻、跟踪池规模、口径说明。

    T-028：原"生成于"实为渲染时刻，易被误读为数据时间——拆成 data_as_of（数据截至，
    库内最新快照 captured_at 的北京日期，UTC+8；采集固定北京 05:00 = UTC 21:00）与
    generated（页面渲染时刻）两段；模板改显三段（数据截至 / 每日采集时刻 / 页面渲染于），
    "生成于"字样删除。
    """
    nominal = _NOMINAL_DAYS.get(period)
    window = None
    if nominal is not None:
        start = as_of_date - timedelta(days=nominal)
        window = f"{start.isoformat()} → {as_of_date.isoformat()}（{nominal} 天）"
    tracked = conn.execute("SELECT COUNT(*) FROM repos WHERE dead = 0").fetchone()[0]
    # T-028：数据截至 = MAX(captured_at) 的北京日期——captured_at 定长 ISO（schema 硬约定，
    # 与 _endpoint_note 同数据源）；采集在 UTC 21:00（北京 05:00）落库，UTC 日期比北京慢一天，
    # 直接切前 10 位会让"数据截至"永远显示前一天（用户必误会），故 +8h 再取日期
    latest_captured = conn.execute("SELECT MAX(captured_at) FROM star_snapshots").fetchone()[0]
    beijing = timezone(timedelta(hours=8))  # 本人自用（中国时区），明示避免与库内 UTC 混淆
    return {
        "window": window,  # 总星榜无窗口概念 → None，模板改显"数据截至"
        "as_of": as_of_date.isoformat(),  # 期次口径基准日（模板不再直接渲染，保留供调试/后续用）
        "data_as_of": (
            (datetime.strptime(latest_captured, _ISO_FMT) + timedelta(hours=8)).date().isoformat()
            if latest_captured
            else "暂无快照"
        ),
        "generated": datetime.now(beijing).strftime("%Y-%m-%d %H:%M"),  # 页面渲染时刻（UTC+8），非数据时间
        "tracked": f"{tracked:,}",
        "method": "增量 = 两端快照差" if nominal is not None else "按最新快照总星数降序",
    }


def _fallback_notice(conn: sqlite3.Connection, period: str, label: str, as_of_date: date, has_total_rows: bool) -> str:
    """空态提示（流程说明 §4：空态必须回答"接下来会发生什么"）。"""
    if not has_total_rows:
        return f"{label} 暂无可展示数据：该期结束前跟踪池尚无任何快照。"
    days = _NOMINAL_DAYS[period]
    unit = "周报" if period == "week" else "季度回顾"
    first_raw = conn.execute("SELECT MIN(created_at) FROM repos").fetchone()[0]
    if first_raw is not None:
        ready = date.fromisoformat(first_raw[:10]) + timedelta(days=days)
        if ready > as_of_date:
            # §4 首期口径：部署未满一个统计窗口 → 总星榜 + 倒计时提示
            return f"首份{unit}预计将于 {ready.month}月{ready.day}日 生成（需满 {days} 天统计窗口），当前为初始总星榜。"
    return f"{label} 增量榜全部缺席（统计窗口内快照不足），当前显示总星榜。"


def _endpoint_snapshot(conn: sqlite3.Connection, repo_id: int, before: str) -> sqlite3.Row | None:
    """dead 关注/标签页仓库的端点快照（stars＋captured_at）：与 app.report._ENDPOINT_SQL 同一主键索引 seek 模式（每仓库一次 LIMIT 1）。

    T-020 起连 captured_at 一并取（行面板端点日期标注的数据源）；None = 该仓 as_of 前无快照。
    """
    return conn.execute(
        "SELECT stars, captured_at FROM star_snapshots WHERE repo_id = ? AND captured_at <= ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (repo_id, before),
    ).fetchone()


def _follow_cards(conn: sqlite3.Connection, follow_rows: list[sqlite3.Row], as_of: str) -> list[dict]:
    """P6 我的关注页行数据（流程说明 §2A）：增量取当期周报同窗口周口径；dead 仓库被 compute_repo_deltas 剔除，端点星数单独补取。

    中-2 转办落地（T-009）：只对关注集算增量（repo_ids 过滤），不再全池复算——
    6.4 万仓全池逐仓端点查询 ≈ 秒级/页，关注集通常个位数到几十。
    """
    if not follow_rows:
        return []
    deltas = compute_repo_deltas(conn, period="week", as_of=as_of, repo_ids=[r["id"] for r in follow_rows])
    cards = []
    for r in follow_rows:
        info = deltas.get(r["id"])
        endpoint = None if info is not None else _endpoint_snapshot(conn, r["id"], as_of)  # dead 仓旁路补取
        stars = info.stars if info is not None else (None if endpoint is None else endpoint["stars"])
        delta = info.delta if info is not None else None
        # T-020：端点日期标注数据源——出席/缺席仓取 deltas 透传值，dead 仓取旁路快照；两路都可能是 None（防御）
        captured_at = info.captured_at if info is not None else (None if endpoint is None else endpoint["captured_at"])
        window_note = None
        if info is not None and info.window_days is not None and abs(info.window_days - _NOMINAL_DAYS["week"]) > 0.05:
            # 与榜单行同一标注口径（决策 4：窗口异常必须标注实际天数）
            window_note = f"实际窗口 {info.window_days:g} 天（取最近可得两端快照）"
        cards.append(
            {
                "full_name": r["full_name"],
                "language": r["language"],
                "description_en": r["description_en"],
                "dead": bool(r["dead"]),
                "delta": delta,  # None → 首周缺席/dead：up 列退化灰字（排序时沉组尾）
                "delta_text": _fmt_delta(delta) if delta is not None else None,
                "delta_neg": delta is not None and delta < 0,
                "stars_text": None if stars is None else _fmt_stars(stars),
                "endpoint_note": _endpoint_note(captured_at),
                "window_note": window_note,
                "created_year": _created_year(r["github_created_at"]),  # T-033：创建年份小灰字数据源
            }
        )
    return cards


def _follow_groups(cards: list[dict], reasons: dict, zh: dict, tags: dict, summaries: dict) -> list[dict]:
    """P6 分组区（流程说明 §2A）：按语言分 7 组（classify_language 同口径；空组不进结果即不渲染）。

    组内三态排序：正常行按当周增量降序 → 无增量行（"—— 下周起有数据"）→ dead 行（灰显"已失效"）沉尾；
    组间按组内最大当周增量降序（全是无增量/dead 的组按 0 沉后，稳定排序保持关注先后）。
    """
    by_lang: dict[str, list[dict]] = {}
    for c in cards:
        by_lang.setdefault(classify_language(c["language"]), []).append(c)
    groups = []
    for key, rows in by_lang.items():
        normal = sorted((r for r in rows if not r["dead"] and r["delta"] is not None), key=lambda r: -r["delta"])
        na = [r for r in rows if not r["dead"] and r["delta"] is None]
        dead = [r for r in rows if r["dead"]]
        view_rows = []
        for i, r in enumerate(normal + na + dead):
            up_na_text = None
            if r["dead"]:
                up_na_text = "——"
            elif r["delta_text"] is None:
                up_na_text = "—— 下周起有数据"
            view_rows.append(
                {
                    "rank": i + 1,  # 组内序号（示意图口径：rank 为组内排名）
                    "full_name": r["full_name"],
                    "language": r["language"],
                    "lang_color": LANG_COLORS.get(r["language"] or "", _DEFAULT_LANG_COLOR),
                    "dead": r["dead"],
                    "delta_text": r["delta_text"],
                    "delta_neg": r["delta_neg"],
                    "up_na_text": up_na_text,
                    "stars_text": r["stars_text"],
                    "followed": True,  # P6 行恒为已关注（金色★ on；点击即取消并移除该行）
                    "description_en": r["description_en"],
                    "description_zh": zh.get(r["full_name"]),
                    "reason": reasons.get(r["full_name"]),  # None → 无推荐语块（AI 降级形态）
                    "summary": summaries.get(r["full_name"]),  # None → 无概要块（AI 降级形态，T-024）
                    "tags": tags.get(r["full_name"], []),
                    "endpoint_note": r["endpoint_note"],
                    "window_note": r["window_note"],
                    "created_year": r["created_year"],  # T-033：创建年份小灰字数据源（None → 不渲染）
                    # T-017（§8.1/§8.2）：关注页长期盯梢语境 → 总星维度文本；行内按钮按 total 操作
                    "reason_dim": "total",
                    "reason_period_label": "all",
                    "reason_label": _reason_label("total", "all"),
                    "show_recommend": True,
                }
            )
        groups.append(
            {
                "anchor": f"f-{key}",
                "label": "其它语言" if key == OTHER_LANGUAGE_KEY else _LANG_KEY_TO_NAME[key],
                "best": normal[0]["delta"] if normal else 0,  # 组间排序键：组内最大增量；无增量/dead 组按 0
                "rows": view_rows,
            }
        )
    groups.sort(key=lambda g: -g["best"])
    return groups


def _all_tags(conn: sqlite3.Connection) -> list[str]:
    """T-022 打标输入建议（datalist）：全部既有标签一次注入（SELECT DISTINCT 去重排序，几十个量级）。

    供渲染行页面的模板上下文（_boards_context / follows_page / tag_page）——每页渲染一个
    `<datalist id="all-tags">`（base.html 统一落点，空库不渲染），行内输入框 list 属性指向它。
    标签云页（无行列表）不注入。
    """
    return [r["tag"] for r in conn.execute("SELECT DISTINCT tag FROM tags ORDER BY tag")]


def _display_maps(
    conn: sqlite3.Connection, *, dimension: str, period_label: str
) -> tuple[dict, dict, dict, dict]:
    """详情面板展示映射（recommendations / description_zh / tags 三张表全部只读）：
    - 推荐理由 recommendations：按 (维度, 期次标签) 取（§8.1 展示映射）——周页 ('week', 当周标签)、
      季页 ('quarter', 当季标签)、总星榜/关注页/标签结果页 ('total', 'all')；缺则该行无推荐语块（AI 降级）；
    - AI 概要 recommendations：固定取 dimension='summary' 且 period_label='all'（T-024 §11：文档视角、
      无维度概念、全页面同一条），缺则该行无概要块（AI 降级形态，不留空框）；
    - 中文描述 description_zh：懒写入，当前全 NULL → 只显示英文（AI 降级口径，流程说明 §4）；
    - 标签 tags：每行 chips 数据源（行内增删走 /api/tags、结果页跳转 /tags/<tag>，T-010 接线）。
    """
    reasons = dict(
        conn.execute(
            "SELECT r.full_name, c.text FROM recommendations c JOIN repos r ON r.id = c.repo_id"
            " WHERE c.dimension = ? AND c.period_label = ?",
            (dimension, period_label),
        ).fetchall()
    )
    summaries = dict(
        conn.execute(
            "SELECT r.full_name, c.text FROM recommendations c JOIN repos r ON r.id = c.repo_id"
            " WHERE c.dimension = 'summary' AND c.period_label = 'all'"
        ).fetchall()
    )
    zh = dict(conn.execute("SELECT full_name, description_zh FROM repos WHERE description_zh IS NOT NULL").fetchall())
    tags: dict[str, list[str]] = {}
    for row in conn.execute("SELECT r.full_name, t.tag FROM tags t JOIN repos r ON r.id = t.repo_id ORDER BY t.tag"):
        tags.setdefault(row["full_name"], []).append(row["tag"])
    return reasons, zh, tags, summaries


def _reason_label(dimension: str, period_label: str) -> str:
    """推荐语块维度标题（2026-08-12 本人复验反馈钉死）：一眼可辨该文本属三维度中的哪套
    （周/季/总星各行一条）；首期空态降级页面上同时解释"当前展示的是总星榜文本"。"""
    if dimension == "week":
        return f"周榜推荐语 · {period_label}"
    if dimension == "quarter":
        return f"季榜推荐语 · {period_label}"
    return "总星榜推荐语"


def _page_recommend_ctx(period: str, as_of_date: date) -> dict:
    """页面展示维度 → 推荐语 (dimension, period_label)（§8.1 展示映射）＋行内按钮参数。

    period 用实际展示期次（周/季页首期空态降级为 total 时按 total 取——页面行即总星榜行，
    按钮维度与展示一致）。周/季按页面语境日期推导标签：历史周页取该周标签，手动重生写回同一期。
    返回 dict 含 reason_dim/reason_period_label/reason_label/show_recommend：reason_label 为
    _reason_label 生成的块标题（周/季带期次、总星固定文案），页面渲染与测试断言均按此取。
    """
    if period == "week":
        label = _week_label(as_of_date)
        return {"reason_dim": "week", "reason_period_label": label, "reason_label": _reason_label("week", label), "show_recommend": True}
    if period == "quarter":
        label = _quarter_label(as_of_date)
        return {"reason_dim": "quarter", "reason_period_label": label, "reason_label": _reason_label("quarter", label), "show_recommend": True}
    return {"reason_dim": "total", "reason_period_label": "all", "reason_label": _reason_label("total", "all"), "show_recommend": True}


def _row_view(
    rank: int,
    row: ReportRow,
    *,
    period: str,
    followed: set,
    reasons: dict,
    summaries: dict,
    zh: dict,
    tags: dict,
    reason_dim: str = "total",
    reason_period_label: str = "all",
    reason_label: str = "总星榜推荐语",
    show_recommend: bool = False,
) -> dict:
    """榜单行 + 详情面板的模板视图：模板只负责渲染，一切格式化在本层完成。

    reason_dim/reason_period_label/reason_label/show_recommend 为 T-017 行内推荐语按钮参数与
    推荐语块维度标题（§8.2：只在有推荐语展示位的页面出现，按当前页维度操作）；标签结果页行
    （_tag_row_view）传 show_recommend=False 不渲染按钮。
    """
    delta_text = None
    delta_neg = False
    if period != "total" and row.delta is not None:  # 出席行 delta 恒非 None；None 防御性保留
        delta_text = _fmt_delta(row.delta)
        delta_neg = row.delta < 0
    window_note = None
    nominal = _NOMINAL_DAYS.get(period)
    if nominal is not None and row.window_days is not None and abs(row.window_days - nominal) > 0.05:
        # 决策 4：窗口异常（滑动取数/采集空洞）必须标注实际天数
        window_note = f"实际窗口 {row.window_days:g} 天（取最近可得两端快照）"
    return {
        "rank": rank,
        "full_name": row.full_name,
        "language": row.language,
        "lang_color": LANG_COLORS.get(row.language or "", _DEFAULT_LANG_COLOR),
        "dead": False,  # 榜单行无 dead/无增量形态（dead 剔除、首周缺席不上榜）；P6 行才有，供 _row.html 分支
        "up_na_text": None,
        "delta_text": delta_text,  # None → 总星榜行不渲染增量列（无增量概念，非"——"）
        "delta_neg": delta_neg,
        "stars_text": _fmt_stars(row.stars),
        "followed": row.full_name in followed,
        "description_en": row.description_en,
        "description_zh": zh.get(row.full_name),
        "reason": reasons.get(row.full_name),  # None → 无推荐语块（AI 降级形态，不留空框）
        "summary": summaries.get(row.full_name),  # None → 无概要块（AI 降级形态，不留空框，T-024）
        "tags": tags.get(row.full_name, []),
        "endpoint_note": _endpoint_note(row.captured_at),
        "window_note": window_note,
        "created_year": row.created_year,  # T-033：创建年份小灰字数据源（None → 模板不渲染标注）
        "reason_dim": reason_dim,
        "reason_period_label": reason_period_label,
        "reason_label": reason_label,
        "show_recommend": show_recommend,
    }


def _rising_row_view(rank: int, row: RisingRow, **row_ctx) -> dict:
    """新崛起区行视图（T-018 决策 4 v2）：复用 _row.html 行形态，增量列显示在池增量＋在池天数。

    与主榜行差异：delta_text = "+X（入池 N 天）"（N 为在池天数整数化，report.pool_days_label 统一取值）；
    不渲染 window_note（新区无主榜滑动窗口概念，区头小字说明已讲清口径）。
    """
    return {
        "rank": rank,
        "full_name": row.full_name,
        "language": row.language,
        "lang_color": LANG_COLORS.get(row.language or "", _DEFAULT_LANG_COLOR),
        "dead": False,
        "up_na_text": None,
        "delta_text": f"{_fmt_delta(row.pool_delta)}（入池 {pool_days_label(row.pool_days)} 天）",
        "delta_neg": row.pool_delta < 0,
        "stars_text": _fmt_stars(row.stars),
        "followed": row.full_name in row_ctx["followed"],
        "description_en": row.description_en,
        "description_zh": row_ctx["zh"].get(row.full_name),
        "reason": row_ctx["reasons"].get(row.full_name),  # None → 无推荐语块（AI 降级形态，不留空框）
        "summary": row_ctx["summaries"].get(row.full_name),  # None → 无概要块（AI 降级形态，不留空框，T-024）
        "tags": row_ctx["tags"].get(row.full_name, []),
        "endpoint_note": _endpoint_note(row.captured_at),
        "window_note": None,
        "created_year": row.created_year,  # T-033：创建年份小灰字数据源（None → 模板不渲染标注）
        "reason_dim": row_ctx["reason_dim"],
        "reason_period_label": row_ctx["reason_period_label"],
        "reason_label": row_ctx["reason_label"],
        "show_recommend": row_ctx["show_recommend"],
    }


def _board_view(board: Board, **row_ctx) -> dict:
    return {
        # kind 必须入锚点：language 榜的 other 与 topic 榜的 other 撞 key
        "anchor": f"b-{board.kind}-{board.key}",
        "label": board.label,
        # T-021 边栏点位色（§12.1）：语言榜用语言点色（与行 badge 呼应），主题榜统一灰点（原型口径）
        "dot": LANG_COLORS.get(_LANG_KEY_TO_NAME.get(board.key, ""), _DEFAULT_LANG_COLOR)
        if board.kind == "language"
        else _DEFAULT_LANG_COLOR,
        "rows": [_row_view(i + 1, r, **row_ctx) for i, r in enumerate(board.rows)],
        # T-018 新区：小字说明按页面期次取名义窗口天数（周 7/季 90）；total 口径新区恒空不渲染
        "rising_days": _NOMINAL_DAYS.get(row_ctx["period"]),
        "rising_rows": [_rising_row_view(i + 1, r, **row_ctx) for i, r in enumerate(board.rising_rows)],
        # T-033 新项目区（决策 5）：行复用 _row_view（与主榜行同形态，增量列 = 窗口增量）；
        # main_note 为主榜区头文案（周/季"按本期增星排序"、total"按总星排序"，§16.1 对仗区头）
        "fresh_rows": [_row_view(i + 1, r, **row_ctx) for i, r in enumerate(board.fresh)],
        "main_note": "按本期增星排序" if row_ctx["period"] != "total" else "按总星排序",
    }


# ===== T-026 单榜整页（§13.1）：board 查询参数解析与边栏整页链接 =====

_BOARD_ALL = "all"  # board=all → 全量 17 榜（保留全量入口，边栏顶部"全部"项）


def _board_key_whitelist(topic_table: dict) -> frozenset[str]:
    """17 榜 key 白名单（§13.1）：语言 7 + 主题（词表数 + 1），与 compute_boards 返回的 kind-key 同形态。

    词表增删主题时榜数随之变化，白名单动态跟随（不写死 17）。
    """
    keys = [f"language-{k}" for k in (*LANGUAGES.values(), OTHER_LANGUAGE_KEY)]
    keys += [f"topic-{k}" for k in (*topic_table, OTHER_TOPIC_KEY)]
    return frozenset(keys)


def _resolve_board(raw: str | None, topic_table: dict) -> str:
    """board 查询参数解析（§13.1）：合法榜 key → 该榜；all → 全量；缺省/非法 → 默认首榜（静默降级不报错）。

    默认首榜 = 语言组第一榜（language-java，LANGUAGES 顺序首项）。
    """
    if raw == _BOARD_ALL:
        return _BOARD_ALL
    if raw is not None and raw in _board_key_whitelist(topic_table):
        return raw
    return f"language-{next(iter(LANGUAGES.values()))}"


def _sidebar_items(boards: list[Board], *, base: str, period_query: str, current: str) -> list[dict]:
    """边栏项数据（T-026 §13.1 起边栏项为整页链接，原页内锚点 scrollspy 移除，active 由服务端渲染）。

    - base/period_query：整页 URL 形态——P1/P2 `/?board=xxx`（历史周携带 `week=...`）、
      P3 `/quarter?board=xxx`（携带 `quarter=...`）、P4 `/total?board=xxx`（无期次）；
    - current：当前榜 key 或 "all"（全量/降级页当前为全部项）；
    - 结构：顶部"全部"项（样式同 sb-item，灰点色，徽标=榜总数）→ 语言组头 + 7 项 → 主题组头 + 10 项；
    - 徽标 = 单榜模式非当前榜用 Board.count（只计数不建行），全量模式 len(rows)——两边栏数据同源同语义。
    """
    items = [
        {
            "key": _BOARD_ALL,
            "label": "全部",
            "dot": _DEFAULT_LANG_COLOR,
            "count": len(boards),
            "active": current == _BOARD_ALL,
            "href": f"{base}?board={_BOARD_ALL}" + (f"&{period_query}" if period_query else ""),
        }
    ]
    last_kind: str | None = None
    for b in boards:
        kind_label = "语言" if b.kind == "language" else "主题"
        if kind_label != last_kind:
            items.append({"group": kind_label})
            last_kind = kind_label
        key = f"{b.kind}-{b.key}"
        dot = (
            LANG_COLORS.get(_LANG_KEY_TO_NAME.get(b.key, ""), _DEFAULT_LANG_COLOR)
            if b.kind == "language"
            else _DEFAULT_LANG_COLOR
        )
        items.append(
            {
                "key": key,
                "label": b.label,
                "dot": dot,
                "count": b.count if b.count is not None else len(b.rows),
                "active": current == key,
                "href": f"{base}?board={key}" + (f"&{period_query}" if period_query else ""),
            }
        )
    return items


def _boards_context(
    request: Request,
    *,
    period: str,
    label: str,
    as_of: str,
    as_of_date: date,
    now: datetime,
    show_sidebar: bool,
    board: str | None = None,
) -> dict:
    """榜单四页共用的上下文装配：榜单（含首期空态降级）→ 元信息 → 期次控件 → 边栏。

    show_sidebar（T-021 §12.1，v2.1 修订）：榜单四页全部 True → 模板以左侧边栏替代顶部 chips 区
    （P4 总星页 v2.1 起纳入，其顶部 chips 与榜头回顶部随之移除）。

    board（T-026 §13.1）：board 查询参数解析结果——"all" 全量 17 榜；具体榜 key 单榜模式
    （当前榜查全内容，其余 15 榜只 count 喂边栏徽标）；缺省/非法已静默降级为默认首榜；
    首期空态降级页维持全量现状（不单榜化），board 参数忽略、当前边栏项为"全部"。

    DB 连接请求级获取/关闭（不持全局长连接）；WAL 下读榜单不阻塞每日采集写入。
    """
    topic_table = _topic_table()
    board_key = _resolve_board(board, topic_table)
    conn = get_conn()
    try:
        # T-027 榜单预计算：仅当前期次尝试读缓存（week 时 label==当前周标签、quarter 时 label==当前季标签、
        # total 恒当前）；历史期次永远实时算、不查表（其 as_of 与缓存采集时刻不同口径）。
        # 命中缓存（全量 17 榜、Board.count 恒 None）直接反序列化使用，跳过 compute_boards 全池逐仓 seek；
        # load 返回 None（三条降级判定任一不过）→ 走原 compute_boards 路径（含 full_keys 单榜收窄），代码保持原样。
        # 单榜模式不需要补 count：模板边栏徽标取 b.count if b.count is not None else len(b.rows)，
        # 缓存 rows 已截 top_n，len(rows)=min(出席数,top_n) 与单榜实时路径的 count 同值，徽标不漂移。
        use_cache = period == "total" or (
            period == "week" and label == week_label(now.date())
        ) or (
            period == "quarter" and label == quarter_label(now.date())
        )
        cache_label = label if period != "total" else "all"
        boards = load_board_cache(conn, period=period, label=cache_label) if use_cache else None
        if (
            boards is not None
            and board_key != _BOARD_ALL
            and not any(f"{b.kind}-{b.key}" == board_key for b in boards)
        ):
            # k3 评审 F2-1：词表/语言集变更并重启后、次日预计算前的窗口期，_resolve_board 按新词表放行
            # 新榜 key，而缓存 payload 是预计算时刻的旧词表——单榜 URL 查无此榜会 StopIteration 500。
            # 按本函数"缓存答不了→实时算"的既有哲学置 None 降级（下方走原 compute_boards 路径）
            boards = None
        if boards is None:
            boards = compute_boards(
                conn,
                topic_table,
                period=period,
                as_of=as_of,
                full_keys=None if board_key == _BOARD_ALL else {board_key},
            )
        notice = None
        effective_period = period
        if period in _NOMINAL_DAYS and all(
            not b.rows and not (b.count or 0) and not b.rising_rows and not b.fresh for b in boards
        ):
            # 首期空态降级（流程说明 §4，T-018 起主榜＋新区全空才降级；T-033 起含新项目区：三区全空才降级）：
            # 增量榜全缺席 → 显示总星榜 + 顶部提示条
            # （判定原样跑在缓存 boards 上：缓存 count 恒 None，本式退化为全量模式原判定；单榜模式
            #   count 参与判定恢复两模式恒等（k3 初审 F2-1）：全量模式 count 恒 None，not (b.count or 0)
            #   恒真，本式退化为原判定；单榜模式非当前榜 count=min(归桶出席数, top_n)，count>0 ⟺ 归桶非空
            #   ⟺ 全量模式该榜 rows 非空（bucket 非空则 _top 至少 1 行）——两模式"主榜＋新区全空"逐榜等价，
            #   单榜空榜 URL 不再误触发降级。单榜模式非当前榜 fresh 恒空列表（T-033 决策 6）、当前榜 fresh
            #   全量——全库无出席仓时新项目区必然全空，not b.fresh 恒真，判定等价性不受 fresh 影响）
            # T-027 降级：先试 total 缓存（仅当前期次——其 as_of≈now、同库状态等价）；None 或历史期次
            # 才实时算（历史期次降级须按请求 as_of 口径，不能用采集时刻缓存）
            cached_total = load_board_cache(conn, period="total", label="all") if use_cache else None
            if cached_total is not None:
                boards = cached_total
            else:
                boards = compute_boards(conn, topic_table, period="total", as_of=as_of)
            effective_period = "total"
            board_key = _BOARD_ALL  # 降级页维持全量现状（§13.1）：board 参数忽略，当前边栏项="全部"
            notice = _fallback_notice(conn, period, label, as_of_date, any(b.rows for b in boards))

        # 关注集只取两处展示用途：行内星标 on/off 态、顶栏"我的关注"计数徽标（v1.3 起关注区在 P6 独立页）
        follow_rows = conn.execute("SELECT r.full_name FROM follows f JOIN repos r ON r.id = f.repo_id").fetchall()
        followed = {r["full_name"] for r in follow_rows}

        # T-017 展示映射（§8.1）：按实际展示期次取 (dimension, period_label)——首期空态降级为 total
        # 时按 total 取（页面行即总星榜行，按钮维度与展示一致）；T-024 概要固定取 ('summary', 'all')
        rec_ctx = _page_recommend_ctx(effective_period, as_of_date)
        reasons, zh, tags, summaries = _display_maps(
            conn, dimension=rec_ctx["reason_dim"], period_label=rec_ctx["reason_period_label"]
        )
        row_ctx = {
            "period": effective_period,
            "followed": followed,
            "reasons": reasons,
            "summaries": summaries,
            "zh": zh,
            "tags": tags,
            "reason_dim": rec_ctx["reason_dim"],
            "reason_period_label": rec_ctx["reason_period_label"],
            "reason_label": rec_ctx["reason_label"],
            "show_recommend": rec_ctx["show_recommend"],
        }
        if board_key == _BOARD_ALL:
            lang_boards = [_board_view(b, **row_ctx) for b in boards if b.kind == "language"]
            topic_boards = [_board_view(b, **row_ctx) for b in boards if b.kind == "topic"]
        else:
            # 单榜模式（§13.1）：只对当前榜装配行视图（其余榜仅 count 已喂边栏徽标，不做行 view/渲染）
            target = next(b for b in boards if f"{b.kind}-{b.key}" == board_key)
            view = _board_view(target, **row_ctx)
            lang_boards = [view] if target.kind == "language" else []
            topic_boards = [view] if target.kind == "topic" else []

        # T-026 边栏整页链接（§13.1）：历史周页携带 week=、季页携带 quarter=、最新周与总星页不带期次
        period_query = ""
        if period == "week" and label != _week_label(now.date()):
            period_query = f"week={label}"
        elif period == "quarter":
            period_query = f"quarter={label}"
        base = {"week": "/", "quarter": "/quarter", "total": "/total"}[period]
        sidebar = _sidebar_items(boards, base=base, period_query=period_query, current=board_key)

        switcher = None
        if period in _NOMINAL_DAYS:
            earliest = _earliest_labels(conn)
            earliest_label = (earliest[0] if period == "week" else earliest[1]) if earliest else label
            switcher = _switcher(period, label, now, earliest_label)

        if period == "week" and label != _week_label(now.date()):
            title = f"历史周报 {label}"
        else:
            title = {"week": "本周报告", "quarter": "季度回顾", "total": "总星榜"}[period]
        return {
            "request": request,
            "page": period,  # 顶栏 active 态：历史周次仍归属"本周报告"
            "title": title,
            "switcher": switcher,
            "show_sidebar": show_sidebar,  # T-021 §12.1（v2.1）：榜单四页 True → 左侧边栏替代顶部 chips
            "meta": _meta(conn, period, as_of_date),  # 窗口/口径按请求期次展示，不因降级改写成总星榜口径
            "notice": notice,
            "follow_count": len(follow_rows),
            "all_tags": _all_tags(conn),  # T-022 打标输入建议（datalist）
            "sidebar": sidebar,  # T-026 §13.1：边栏项整页链接（含"全部"项、服务端 active）
            "lang_boards": lang_boards,
            "topic_boards": topic_boards,
        }
    finally:
        conn.close()


# ===== 路由 =====


@router.get("/", response_class=HTMLResponse)
def weekly(request: Request, week: str | None = None, board: str | None = None) -> HTMLResponse:
    """P1 本周报告（=最新一期）；?week=2026-W32 回看历史周次（P2 与 P1 同页换期次，流程说明 §1）。

    T-021 §12.1（v2.1）：榜单四页边栏 = True（首期空态降级为 total 榜单时页面仍是周报语境，边栏保留）。
    T-026 §13.1：?board=xxx 单榜整页（缺省/非法静默降级默认首榜；board=all 全量；历史周携带 week=）。
    """
    now = datetime.now(timezone.utc)
    label, as_of, as_of_date = _resolve_week(week, now)
    return templates.TemplateResponse(
        request=request,
        name="boards.html",
        context=_boards_context(
            request,
            period="week",
            label=label,
            as_of=as_of,
            as_of_date=as_of_date,
            now=now,
            show_sidebar=True,
            board=board,
        ),
    )


@router.get("/quarter", response_class=HTMLResponse)
def quarterly(request: Request, quarter: str | None = None, board: str | None = None) -> HTMLResponse:
    """P3 季度回顾（90 天增量榜）；?quarter=2026-Q3 回看往期。T-021：边栏 = True（同周报页）。

    T-026 §13.1：?board=xxx 单榜整页（携带当前 quarter=）。
    """
    now = datetime.now(timezone.utc)
    label, as_of, as_of_date = _resolve_quarter(quarter, now)
    return templates.TemplateResponse(
        request=request,
        name="boards.html",
        context=_boards_context(
            request,
            period="quarter",
            label=label,
            as_of=as_of,
            as_of_date=as_of_date,
            now=now,
            show_sidebar=True,
            board=board,
        ),
    )


@router.get("/total", response_class=HTMLResponse)
def total(request: Request, board: str | None = None) -> HTMLResponse:
    """P4 总星榜：最新快照总星数降序 Top 50/榜（T-028：默认 top_n 30→50），无期次概念。

    T-021 §12.1（v2.1 修订）：P4 纳入边栏（实测同为 17 榜长页、HTML 1.7MB 全站最重），
    其顶部 chips 区与榜头回顶部随边栏到位同步移除（与 P1/P2/P3 一致）。
    T-026 §13.1：?board=xxx 单榜整页（无期次参数）。
    """
    now = datetime.now(timezone.utc)
    return templates.TemplateResponse(
        request=request,
        name="boards.html",
        context=_boards_context(
            request,
            period="total",
            label="总星榜",
            as_of=now.strftime(_ISO_FMT),
            as_of_date=now.date(),
            now=now,
            show_sidebar=True,
            board=board,
        ),
    )


@router.get("/follows", response_class=HTMLResponse)
def follows_page(request: Request) -> HTMLResponse:
    """P6 我的关注（v1.3 独立页）：关注仓库按语言分组，增量与当期周报同窗口（as_of=now 周口径）。

    无期次概念（不跟周次切换走）；空关注时渲染空态引导（流程说明 §4），分组/排序口径在 _follow_groups。
    T-021 §12.2：filter_tags = 我的全部标签（含 0 计数，字典序），供页内客户端筛选 chips。
    """
    now = datetime.now(timezone.utc)
    conn = get_conn()
    try:
        follow_rows = conn.execute(
            "SELECT r.id, r.full_name, r.language, r.description_en, r.dead, r.github_created_at"
            " FROM follows f JOIN repos r ON r.id = f.repo_id ORDER BY f.created_at"
        ).fetchall()
        cards = _follow_cards(conn, follow_rows, now.strftime(_ISO_FMT))
        # T-017（§8.1）：关注页长期盯梢语境 → 总星维度文本（最新一条），缺则该行无推荐语块；
        # T-024：概要固定取 ('summary', 'all')（文档视角，全页面同一条），缺则该行无概要块
        reasons, zh, tags, summaries = _display_maps(conn, dimension="total", period_label="all")
        # T-021 §12.2 筛选 chips：我的全部标签各自带"关注仓中打该标签的数量"（LEFT JOIN follows，
        # 0 计数也要列出供空态演示）；按标签名字典序（与 _all_tags 同口径 ORDER BY tag）
        filter_tags = [
            {"tag": r["tag"], "count": r["n"]}
            for r in conn.execute(
                "SELECT t.tag AS tag, COUNT(f.repo_id) AS n FROM tags t"
                " LEFT JOIN follows f ON f.repo_id = t.repo_id GROUP BY t.tag ORDER BY t.tag"
            )
        ]
        return templates.TemplateResponse(
            request=request,
            name="follows.html",
            context={
                "request": request,
                "page": "follows",  # 顶栏 active 态
                "title": "我的关注",
                "meta": _meta(conn, "week", now.date()),  # 与当期周报同窗口口径（v1.3 §2A 第 1 层）
                "follow_count": len(follow_rows),
                "filter_tags": filter_tags,  # T-021：标签筛选行数据（含 0 计数）
                "all_tags": _all_tags(conn),  # T-022 打标输入建议（datalist）
                "groups": _follow_groups(cards, reasons, zh, tags, summaries),
            },
        )
    finally:
        conn.close()


@router.get("/topic-candidates", response_class=HTMLResponse)
def topic_candidates_page(request: Request) -> HTMLResponse:
    """P7 候选词只读页（T-032，§15.1）：候选词｜建议主题｜池内出现次数｜最近扫描日期，按次数降序。

    无按钮无写操作（收词走会话拍板＋部署，§15.2）；入口在 P1 元信息行"候选词"小字链接（不占顶栏，
    §15.1——顶栏 5 项上限不破）；两种空态（§15.3）：扫描过但无候选 / 从未成功扫描（AI 未配置）——
    后者凭 candidate_scans 单行记录区分（有记录 = 成功扫描过）。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT term, suggested_topic, pool_count, scanned_at FROM topic_candidates"
            " ORDER BY pool_count DESC, term"
        ).fetchall()
        scan = conn.execute("SELECT scanned_at FROM candidate_scans WHERE id = 1").fetchone()
        view_rows = [
            {
                "term": r["term"],
                "suggested_topic": r["suggested_topic"],
                "rejected": r["suggested_topic"] == NOT_RECOMMENDED,  # "不建议收录"灰显（模板分支）
                "pool_count": r["pool_count"],
                "scan_date": r["scanned_at"][:10],  # 定长 ISO 前 10 位即日期（schema 硬约定，同 _endpoint_note）
            }
            for r in rows
        ]
        follow_count = conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0]
        return templates.TemplateResponse(
            request=request,
            name="topic_candidates.html",
            context={
                "request": request,
                "page": "candidates",  # 顶栏无此项（§15.1 不占顶栏）：任何导航项都不高亮
                "title": "候选词",
                "rows": view_rows,
                "has_scan": scan is not None,  # False = 从未成功扫描（空态注明"扫描未运行过（AI 未配置）"）
                "threshold": POOL_COUNT_THRESHOLD,  # 元信息行口径说明（词频阈值，实施定 5）
                "follow_count": follow_count,
            },
        )
    finally:
        conn.close()


# ===== 关注 API（T-009；决策 9 动态入池；三态分流在 app.follows） =====

# owner/repo 字符集（GitHub 惯例：字母数字与 -_.）；长度上限 = owner 39 + "/" + repo 100
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FULL_NAME_MAX_LEN = 140


async def _github_client() -> AsyncIterator[GitHubClient]:
    """请求级 GitHub 客户端（关注动态入池唯一外呼点）：token 空/无效由客户端在使用点报清晰错误。

    依赖注入形态（Depends）：单测用 app.dependency_overrides 换假 client，不打真 API。
    """
    client = GitHubClient(get_settings().github_token)
    try:
        yield client
    finally:
        await client.aclose()


def _parse_full_name(raw: object) -> str:
    """full_name 入参校验：非法形态直接 400（fail-loud，不把垃圾输入带去打 GitHub）。"""
    if not isinstance(raw, str) or not _FULL_NAME_RE.fullmatch(raw) or len(raw) > _FULL_NAME_MAX_LEN:
        raise HTTPException(status_code=400, detail=f"full_name 应为 owner/repo 形态，收到：{raw!r}")
    return raw


def _parse_as_of(raw: object) -> str | None:
    """as_of 可选入参：缺省 None（取当前 UTC）；给了必须符合 UTC 定长硬约定，否则 400。"""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail=f"as_of 应为 UTC 定长 ISO（YYYY-MM-DDTHH:MM:SSZ），收到：{raw!r}")
    try:
        datetime.strptime(raw, _ISO_FMT)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"as_of 应为 UTC 定长 ISO（YYYY-MM-DDTHH:MM:SSZ），收到：{raw!r}"
        ) from None
    return raw


@router.post("/api/follows")
async def api_follow(request: Request, client: GitHubClient = Depends(_github_client)) -> dict:
    """关注一个仓库（三态：已入池只补 follows / dead 复活＋基线 / 未入池动态入池）。

    请求体 JSON：{"full_name": "owner/repo", "as_of": 可选（UTC 定长 ISO；v1.3 起仅受理校验、不再消费——
    P1 关注区已移出，响应不带 card_html，as_of 不再用于局部渲染）}。
    幂等：重复关注返回 already_followed=true，库内零变化。
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail='请求体须为 JSON：{"full_name": "owner/repo"}') from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='请求体须为 JSON：{"full_name": "owner/repo"}')
    full_name = _parse_full_name(payload.get("full_name"))
    _parse_as_of(payload.get("as_of"))  # 校验形态保留（坏值仍 400）；值本身 v1.3 起不再消费
    conn = get_conn()
    try:
        try:
            outcome = await follow_repo(conn, client, full_name)
        except GitHubNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"GitHub 上找不到仓库 {full_name}（不存在/已删除/转私有）"
            ) from None
        except GitHubAuthError as exc:
            # token 配置问题（服务端侧）：message 自带修复指引，原样透传给本人
            raise HTTPException(status_code=500, detail=str(exc)) from None
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=f"GitHub 请求失败：{exc}") from None
        except ValueError as exc:
            # node_id 撞库（改名仓库）：已回滚无半写，409 让本人知情处置
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {
            "full_name": outcome.full_name,
            "followed": True,
            "state": outcome.state,
            "already_followed": outcome.already_followed,
            "follow_count": conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0],
        }
    finally:
        conn.close()


@router.delete("/api/follows/{full_name:path}")
def api_unfollow(full_name: str) -> dict:
    """取消关注：只删 follows 行（跟踪保留）；未关注/不存在的仓库幂等成功（removed=false）。"""
    full_name = _parse_full_name(full_name)
    conn = get_conn()
    try:
        removed = unfollow_repo(conn, full_name)
        return {
            "full_name": full_name,
            "followed": False,
            "removed": removed,
            "follow_count": conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0],
        }
    finally:
        conn.close()


# ===== 标签 API 与 P5 筛选页（T-010；交互口径《交互流程说明》§3.3/§4） =====

# 标签长度上限（流程说明 §3.3：1~20 字符）：与前端 maxLength=20 同值，服务端兜底
_TAG_MAX_LEN = 20


def _parse_tag(raw: object) -> str:
    """标签入参校验：必须 str，去首尾空格后非空且 ≤20 字符（流程说明 §3.3），否则 400。

    tag 原样存储原样判重（大小写敏感，不做归一化）；去空格只做一次（写入/判重前）。
    """
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail=f"tag 应为字符串，收到：{raw!r}")
    tag = raw.strip()
    if not tag:
        raise HTTPException(status_code=400, detail="标签不能为空（去首尾空格后）")
    if len(tag) > _TAG_MAX_LEN:
        raise HTTPException(status_code=400, detail=f"标签长度不能超过 {_TAG_MAX_LEN} 字符")
    return tag


@router.post("/api/tags")
async def api_add_tag(request: Request) -> dict:
    """给仓库打标签（T-010，§3.3 第 1 步）：同仓同名幂等（added=false，不重复写入→前端 toast"标签已存在"）。

    响应 tags = 该仓全部标签（ORDER BY tag，与 _display_maps 行内 chips 同序）；
    full_name 校验复用 _parse_full_name；仓库不在跟踪池 → 404。
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail='请求体须为 JSON：{"full_name": "owner/repo", "tag": "..."}') from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='请求体须为 JSON：{"full_name": "owner/repo", "tag": "..."}')
    full_name = _parse_full_name(payload.get("full_name"))
    tag = _parse_tag(payload.get("tag"))
    conn = get_conn()
    try:
        repo = conn.execute("SELECT id FROM repos WHERE full_name = ?", (full_name,)).fetchone()
        if repo is None:
            raise HTTPException(status_code=404, detail=f"仓库不在跟踪池：{full_name}")
        cur = conn.execute("INSERT OR IGNORE INTO tags (repo_id, tag) VALUES (?, ?)", (repo["id"], tag))
        conn.commit()
        tags = [r["tag"] for r in conn.execute("SELECT tag FROM tags WHERE repo_id = ? ORDER BY tag", (repo["id"],))]
        return {"added": cur.rowcount > 0, "tags": tags}
    finally:
        conn.close()


@router.delete("/api/tags/{full_name:path}")
def api_delete_tag(full_name: str, tag: str | None = None) -> dict:
    """删除标签（T-010，§3.3 第 2 步）：标签/仓库不存在均幂等 removed=false（与取消关注同口径）。

    形态说明（任务书授权"或等价形态"）：full_name 走 path（:path 天然承载含 / 的仓库名），tag 走 query——
    {full_name:path}/{tag:path} 双 path 参数存在贪婪解析歧义（tag 含 / 时 full_name 会多吃段），
    且 %2F 可能被反代解码；tag 编码进 query 后两者皆无，流程偏差已在交付报告留痕。
    """
    full_name = _parse_full_name(full_name)
    tag = _parse_tag(tag)
    conn = get_conn()
    try:
        repo = conn.execute("SELECT id FROM repos WHERE full_name = ?", (full_name,)).fetchone()
        if repo is None:
            return {"removed": False}  # 仓库不在池：删除目标本就不存在，幂等成功
        cur = conn.execute("DELETE FROM tags WHERE repo_id = ? AND tag = ?", (repo["id"], tag))
        conn.commit()
        return {"removed": cur.rowcount > 0}
    finally:
        conn.close()


def _tag_cloud(conn: sqlite3.Connection) -> list[dict]:
    """标签云数据（P5 总页）：每个标签＋项目数；项目数降序、同数按标签名（任务书口径）。"""
    rows = conn.execute("SELECT tag, COUNT(*) AS n FROM tags GROUP BY tag").fetchall()
    return [
        {"tag": r["tag"], "n": r["n"], "href": f"/tags/{quote(r['tag'], safe='')}"}
        for r in sorted(rows, key=lambda r: (-r["n"], r["tag"]))
    ]


def _tag_row_view(
    rank: int,
    row: sqlite3.Row,
    stars: int | None,
    captured_at: str | None,
    *,
    followed: set,
    reasons: dict,
    summaries: dict,
    zh: dict,
    tags: dict,
) -> dict:
    """标签结果页行视图：与 _row_view 同字段契约（_row.html 共用），无增量列（按总星排序口径）。

    dead 仓库保留展示（打过的标签仍在）：整行灰显＋"已失效"（_row.html 既有分支），up 列退化"——"。
    T-017：推荐语按总星维度展示（同总星榜语境）；行内推荐按钮不渲染（§8.2 只在周/季/总星/关注四页出现）。
    """
    return {
        "rank": rank,
        "full_name": row["full_name"],
        "language": row["language"],
        "lang_color": LANG_COLORS.get(row["language"] or "", _DEFAULT_LANG_COLOR),
        "dead": bool(row["dead"]),
        "up_na_text": "——" if row["dead"] else None,
        "delta_text": None,  # 标签页无增量概念（同总星榜行形态）
        "delta_neg": False,
        "stars_text": None if stars is None else _fmt_stars(stars),
        "followed": row["full_name"] in followed,
        "description_en": row["description_en"],
        "description_zh": zh.get(row["full_name"]),
        "reason": reasons.get(row["full_name"]),  # None → 无推荐语块（AI 降级形态，不留空框）
        "summary": summaries.get(row["full_name"]),  # None → 无概要块（AI 降级形态，不留空框，T-024）
        "tags": tags.get(row["full_name"], []),
        "endpoint_note": _endpoint_note(captured_at),
        "window_note": None,
        "created_year": _created_year(row["github_created_at"]),  # T-033：创建年份小灰字数据源
        "reason_dim": "total",
        "reason_period_label": "all",
        "reason_label": _reason_label("total", "all"),
        "show_recommend": False,
    }


@router.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request) -> HTMLResponse:
    """P5 标签筛选总页（流程说明 §1）：全部标签云；空库渲染空态（引导打标入口）。"""
    conn = get_conn()
    try:
        follow_count = conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0]
        return templates.TemplateResponse(
            request=request,
            name="tags.html",
            context={
                "request": request,
                "page": "tags",  # 顶栏 active 态
                "title": "标签筛选",
                "tag": None,  # None → 模板渲染云视图（结果页才传 tag）
                "tags": _tag_cloud(conn),
                "follow_count": follow_count,
            },
        )
    finally:
        conn.close()


@router.get("/tags/{tag:path}", response_class=HTMLResponse)
def tag_page(request: Request, tag: str) -> HTMLResponse:
    """P5 标签结果页（流程说明 §3.3 第 3 步）：该标签下项目列表，行形态复用 _row.html（紧凑行＋默认展开＋★）。

    排序：最新快照总星降序（与总星榜同口径）；标签集小（个位~几十），逐仓端点查询走主键 seek
    （P6 关注集同模式；schema 硬约束禁止的相关子查询形态仅针对全池榜单 SQL，此处不触发）。
    :path 承载含 / 的标签（href 已 quote 编码；反代若解码 %2F 也不影响语义，贪婪匹配兜底）；
    空结果渲染 §4 空态（"还没有项目打过「xx」标签"＋返回 /tags 链接）。
    """
    tag = tag.strip()
    if not tag:
        # /tags/（尾斜杠）会匹配本路由且 tag=""，走到这里；/tags 本体由精确路由处理
        raise HTTPException(status_code=404)
    now = datetime.now(timezone.utc)
    as_of = now.strftime(_ISO_FMT)
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT r.id, r.full_name, r.language, r.description_en, r.dead, r.github_created_at"
            " FROM tags t JOIN repos r ON r.id = t.repo_id WHERE t.tag = ?",
            (tag,),
        ).fetchall()
        starred = []
        for r in rows:
            endpoint = _endpoint_snapshot(conn, r["id"], as_of)
            stars = None if endpoint is None else endpoint["stars"]
            captured_at = None if endpoint is None else endpoint["captured_at"]
            starred.append((stars, captured_at, r))
        starred.sort(key=lambda t: (-(t[0] or 0), t[2]["full_name"]))  # 无快照行按 0 沉底，同星按名稳定

        # T-017（§8.1）：标签结果页同总星榜语境 → 总星维度文本；行内推荐按钮不渲染（_tag_row_view）
        reasons, zh, tags, summaries = _display_maps(conn, dimension="total", period_label="all")
        follow_rows = conn.execute("SELECT r.full_name FROM follows f JOIN repos r ON r.id = f.repo_id").fetchall()
        followed = {r["full_name"] for r in follow_rows}
        view_rows = [
            _tag_row_view(
                i + 1, row, stars, captured_at, followed=followed, reasons=reasons, summaries=summaries, zh=zh, tags=tags
            )
            for i, (stars, captured_at, row) in enumerate(starred)
        ]
        return templates.TemplateResponse(
            request=request,
            name="tags.html",
            context={
                "request": request,
                "page": "tags",
                "title": f"标签「{tag}」",
                "tag": tag,
                "rows": view_rows,
                "total": len(view_rows),
                "follow_count": len(follow_rows),
                "all_tags": _all_tags(conn),  # T-022 打标输入建议（datalist）
            },
        )
    finally:
        conn.close()


# ===== 翻译 API（T-016；口径《交互流程说明》§7.4 钉死：单个强制重译 / 批量只补 NULL，不触碰 recommendations 表） =====

# 批量补译后台任务状态（§7.2 后台形态，对齐共识§7"后台执行、防重复触发"）：POST 原子占位 running →
# 起 worker 立即 202 → 前端 2s 轮询 /status；error ∈ None | "auth"（key 未配置/无效/余额，确定性配置错误）|
# "unknown"（其余未知异常）；S 档单进程跨请求共享，线程锁保护读写
_batch_state: dict = {"running": False, "translated": 0, "failed": 0, "total": 0, "finished": False, "error": None}
_batch_state_lock = threading.Lock()  # 保护 _batch_state 读写；防重入语义（running 时 POST → 409）沿用 in-flight 锁口径
_batch_task: asyncio.Task | None = None  # 模块级持有后台任务引用，防 GC 意外回收


def _make_ai_client() -> DeepSeekClient:
    """批量 worker 自造 DeepSeek client：后台任务无请求生命周期，不能复用请求级 _ai_client 依赖（响应结束即关闭）；
    单测 monkeypatch 本工厂注入假 client（不走 dependency_overrides）。"""
    return DeepSeekClient(get_settings().deepseek_api_key)


async def _ai_client() -> AsyncIterator[DeepSeekClient]:
    """请求级 DeepSeek 客户端（翻译写端点唯一外呼点）：key 空/无效由客户端在使用点报清晰错误。

    依赖注入形态（Depends）：单测用 app.dependency_overrides 换假 client，不打真 API（同 _github_client）。
    """
    client = DeepSeekClient(get_settings().deepseek_api_key)
    try:
        yield client
    finally:
        await client.aclose()


async def _parse_translate_payload(request: Request) -> str:
    """/api/translate 请求体解析：JSON dict + full_name 形态校验（fail-loud，同 /api/follows 口径）。"""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail='请求体须为 JSON：{"full_name": "owner/repo"}') from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='请求体须为 JSON：{"full_name": "owner/repo"}')
    return _parse_full_name(payload.get("full_name"))


def _translate_error_to_http(exc: Exception) -> HTTPException:
    """AI 调用异常 → HTTP 错误映射：账户类（key 空/无效/余额）→ 500 带修复指引（同关注 API 的 GitHubAuthError 姿态）；
    其余（超时/5xx/畸形）→ 502。失败时库内未写入，旧译文天然保留（§7.3）。
    """
    if isinstance(exc, DeepSeekAuthError):
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(status_code=502, detail=f"翻译失败：{exc}")


@router.post("/api/translate")
async def api_translate(request: Request, client: DeepSeekClient = Depends(_ai_client)) -> dict:
    """单个强制重译（§7.2 第 1 步）：不管现值覆盖写 repos.description_zh，不触碰 recommendations。

    400 两分支的 detail 即 §7.3 前端 toast 文案（"无简介可译" / "原文已是中文，无需翻译"），前端按 status 直用；
    repo 不在跟踪池 → 404；AI/网络失败 → 502（旧译文保留，前端 toast"翻译失败，稍后再试"）。
    """
    full_name = await _parse_translate_payload(request)
    conn = get_conn()
    try:
        repo = conn.execute("SELECT id, description_en FROM repos WHERE full_name = ?", (full_name,)).fetchone()
        if repo is None:
            raise HTTPException(status_code=404, detail=f"仓库不在跟踪池：{full_name}")
        text_en = repo["description_en"]
        if not text_en or not text_en.strip():
            raise HTTPException(status_code=400, detail="无简介可译")
        if has_cjk(text_en):
            raise HTTPException(status_code=400, detail="原文已是中文，无需翻译")
        try:
            zh = await client.translate(text_en)
        except Exception as exc:  # 不写库：失败旧译文保留（§7.3）；HTTP 映射见 _translate_error_to_http
            raise _translate_error_to_http(exc) from None
        conn.execute("UPDATE repos SET description_zh = ? WHERE id = ?", (zh, repo["id"]))
        conn.commit()
        return {"full_name": full_name, "translated": True, "description_zh": zh}
    finally:
        conn.close()


@router.post("/api/translate-missing", status_code=202)
async def api_translate_missing() -> dict:
    """批量补译（§7.2 第 2 步，后台任务形态）：锁内原子检查＋占位 running → 统计待译 total（范围集 S
    = 三口径榜 Top50 去重 ∪ 关注集内 description_zh IS NULL 且英文非空无 CJK 的仓——T-017 决策 5 v3
    收窄，S 之外不送译；SQL 预过滤 NULL/空串，Python 过 has_cjk）→ asyncio.create_task 起 worker（模块级
    持有防 GC）→ 立即返回 202 {"started": true, "total": T}；running 时重复触发 409（detail 即 §7.3 前端 toast
    文案）；不触碰 recommendations 表。进度/结果由 worker 写入 _batch_state，前端轮询 /status 读取。
    """
    global _batch_task
    conn = get_conn()
    try:
        with _batch_state_lock:
            if _batch_state["running"]:
                raise HTTPException(status_code=409, detail="补译任务进行中，请稍后再试")
            # T-017 收窄（决策 5 v3，评审 F1-1）：翻译目标 = 范围集 S（三口径榜 Top50 去重 ∪ 关注集），
            # S 之外（未上榜未关注）的仓不送译——与每日 ensure 翻译段同源口径
            pending = _pending_translate_in_scope(conn)
            _batch_state.update(running=True, translated=0, failed=0, total=len(pending), finished=False, error=None)
    finally:
        conn.close()
    _batch_task = asyncio.create_task(_run_batch_translate(pending))
    return {"started": True, "total": len(pending)}


def _pending_translate_in_scope(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """批量补译候选：范围集 S 内 description_zh IS NULL 且英文非空无 CJK 的仓库（评审 F1-1 收窄）。

    与每日 ensure 翻译段同一范围口径（_scope_sets：三口径榜（主榜 Top50 ∪ 新崛起区 Top10 ∪ 新项目区
    Top20）去重 ∪ 关注集）；空描述/CJK 跳过口径照旧（SQL 预过滤 NULL/空串，Python 过 has_cjk）；
    分块防旧 SQLite 变量上限。
    """
    listed_by_period, follow_names = _scope_sets(conn, now=datetime.now(timezone.utc))
    scope_names = list(follow_names)
    for period in ("week", "quarter", "total"):
        for full_name in listed_by_period[period]:
            if full_name not in scope_names:
                scope_names.append(full_name)
    pending: list[sqlite3.Row] = []
    for offset in range(0, len(scope_names), _NAME_LOOKUP_CHUNK):
        chunk = scope_names[offset : offset + _NAME_LOOKUP_CHUNK]
        placeholders = ", ".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT id, full_name, description_en FROM repos"
            " WHERE full_name IN ({}) AND description_zh IS NULL"
            " AND description_en IS NOT NULL AND trim(description_en) != ''".format(placeholders),
            chunk,
        ).fetchall()
        pending.extend(row for row in rows if not has_cjk(row["description_en"]))
    return pending


async def _run_batch_translate(pending: list[sqlite3.Row]) -> None:
    """后台批量补译 worker（§7.2）：只补 NULL 不覆盖（pending 已在 POST 侧按口径筛好）；自开 DB 连接与 AI client
    （不依赖请求生命周期，finally 双双关闭）；逐条翻译沿用现口径——空描述/CJK 跳过、单条失败记 WARNING 计入 failed
    继续整批、每条 UPDATE＋commit 立即落库；DeepSeekAuthError（key 未配置/无效/余额）是确定性配置错误 →
    error="auth" 终止整批（已译保留）；其余未知异常 → logger.exception＋error="unknown"；
    结束一律 running=False、finished=True（state 保留供查询；下次 POST 时重置计数与 error）。
    """
    conn = client = None
    try:
        conn = get_conn()
        client = _make_ai_client()
        for row in pending:
            text_en = row["description_en"]
            if not text_en or not text_en.strip():
                continue  # GitHub 官方允许无简介：空描述跳过
            if has_cjk(text_en):
                continue  # 原文已含中文（含中英混排），不送译
            try:
                zh = await client.translate(text_en)
            except DeepSeekAuthError as exc:
                # 账户类确定性错误（key 空/无效/余额）：终止整批，已译保留；error="auth" 供前端 toast（§7.3）
                logger.warning("AI 批量补译终止（账户类错误）：%s", exc)
                with _batch_state_lock:
                    _batch_state["error"] = "auth"
                return
            except Exception as exc:
                logger.warning("AI 批量补译失败，跳过 %s：%s", row["full_name"], exc)
                with _batch_state_lock:
                    _batch_state["failed"] += 1
                continue
            # 低-6 收口（T-017 顺带）：UPDATE 带护栏（id + 现值仍 NULL + 原文未变）——
            # 写库前不重查现值的竞态防护（并发单个翻译 API 或当轮 ensure 已写入则本条跳过，不重复计数）
            cur = conn.execute(
                "UPDATE repos SET description_zh = ? WHERE id = ? AND description_zh IS NULL AND description_en = ?",
                (zh, row["id"], text_en),
            )
            conn.commit()
            if cur.rowcount == 0:
                continue  # 并发窗口内已被写入：本条不再计数，重跑幂等自然收敛
            with _batch_state_lock:
                _batch_state["translated"] += 1
    except Exception:
        logger.exception("AI 批量补译异常终止")
        with _batch_state_lock:
            _batch_state["error"] = "unknown"
    finally:
        try:
            if client is not None:
                await client.aclose()
        except Exception:  # 清理失败不阻断终端状态落盘（防假 client 无 aclose 等）
            logger.exception("AI 批量补译 client 关闭失败")
        if conn is not None:
            conn.close()
        with _batch_state_lock:
            _batch_state["running"] = False
            _batch_state["finished"] = True


@router.get("/api/translate-missing/status")
async def api_translate_missing_status() -> dict:
    """批量补译进度查询（§7.2）：前端每 2s 轮询；不依赖 AI client，无任务史时全零/false/None。"""
    with _batch_state_lock:
        return dict(_batch_state)


# ===== 推荐语 API（T-017；口径《交互流程说明》§8.4 钉死：单个强制重生 / 批量只补缺失，不触碰翻译字段） =====

# 批量补齐推荐语后台任务状态（§8.2 后台形态；与翻译批量 _batch_state 并列独立不共用——语义/失败域/
# 状态文案/节奏不同）：POST 原子占位 running → 起 worker 立即 202 → 前端 2s 轮询 /status；
# error ∈ None | "auth"（key 未配置/无效/余额，确定性配置错误）| "unknown"（其余未知异常）；
# S 档单进程跨请求共享，线程锁保护读写
_rec_batch_state: dict = {
    "running": False,
    "recommended": 0,
    "failed": 0,
    "total": 0,
    "finished": False,
    "error": None,
}
_rec_batch_state_lock = threading.Lock()  # 保护 _rec_batch_state 读写；防重入语义（running 时 POST → 409）
_rec_batch_task: asyncio.Task | None = None  # 模块级持有后台任务引用，防 GC 意外回收

_REC_DIMENSIONS = ("week", "quarter", "total")
_NAME_LOOKUP_CHUNK = 500  # 批量补缺反查 id 的分块大小：防御旧编译 SQLite 的 999 变量上限（与 app.report 同口径）


def _make_github_client() -> GitHubClient:
    """批量推荐 worker 自造 GitHub client（README 输入拉取）：后台任务无请求生命周期；
    单测 monkeypatch 本工厂注入假 client（同 _make_ai_client 手法，不打真 API）。"""
    return GitHubClient(get_settings().github_token)


def _validate_recommend_params(dimension: object, period_label: object) -> tuple[str, str]:
    """推荐语维度/期次标签入参校验（fail-loud）：dimension ∈ 三口径；period_label 按维度格式
    （周 ISO / 季 2026-Q3 / total 固定 'all'）。"""
    if dimension not in _REC_DIMENSIONS:
        raise HTTPException(status_code=400, detail=f"dimension 应为 week/quarter/total，收到：{dimension!r}")
    if not isinstance(period_label, str):
        raise HTTPException(status_code=400, detail=f"period_label 应为字符串，收到：{period_label!r}")
    if dimension == "total":
        if period_label != "all":
            raise HTTPException(status_code=400, detail="total 维度期次标签固定为 all")
    elif dimension == "week":
        if not _WEEK_RE.fullmatch(period_label):
            raise HTTPException(status_code=400, detail=f"周次格式应为 ISO 周（如 2026-W32），收到：{period_label!r}")
    else:
        if not _QUARTER_RE.fullmatch(period_label):
            raise HTTPException(status_code=400, detail=f"季度格式应为 2026-Q3，收到：{period_label!r}")
    return dimension, period_label


async def _parse_recommend_payload(request: Request) -> tuple[str, str, str]:
    """/api/recommend 请求体解析：JSON dict + full_name/dimension/period_label 校验（fail-loud）。"""
    hint = '请求体须为 JSON：{"full_name": "...", "dimension": "...", "period_label": "..."}'
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail=hint) from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=hint)
    full_name = _parse_full_name(payload.get("full_name"))
    dimension, period_label = _validate_recommend_params(payload.get("dimension"), payload.get("period_label"))
    return full_name, dimension, period_label


def _as_of_for_label(dimension: str, period_label: str, now: datetime) -> str | None:
    """单个重生按当前页期次换算 as_of（增量语境口径，与 _resolve_week/_resolve_quarter 同姿态）：
    历史周/季取期末 23:59:59Z，当前期取 now（不预支未来）；total 无增量概念返回 None。"""
    if dimension == "week":
        monday = _parse_week_param(period_label)
        if period_label == _week_label(now.date()):
            return now.strftime(_ISO_FMT)
        return f"{(monday + timedelta(days=6)).isoformat()}T23:59:59Z"
    if dimension == "quarter":
        year, q = _parse_quarter_param(period_label)
        if period_label == _quarter_label(now.date()):
            return now.strftime(_ISO_FMT)
        return f"{_quarter_end_date(year, q).isoformat()}T23:59:59Z"
    return None


def _recommend_error_to_http(exc: Exception) -> HTTPException:
    """AI 调用异常 → HTTP 错误映射（同翻译 API 姿态）：账户类（key 空/无效/余额）→ 500 带修复指引；
    其余（超时/5xx/畸形）→ 502。失败时库内未写入，旧文本天然保留（§8.3）。"""
    if isinstance(exc, DeepSeekAuthError):
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(status_code=502, detail=f"推荐语生成失败：{exc}")


@router.post("/api/recommend")
async def api_recommend(
    request: Request,
    client: DeepSeekClient = Depends(_ai_client),
    github: GitHubClient = Depends(_github_client),
) -> dict:
    """单个强制重生（§8.2 第 1 步 / §8.4）：按当前页维度覆盖同维度当期行（INSERT OR REPLACE 新文本）。

    README 正文随生成拉取入输入（失败/空退化元数据输入，不阻塞）；total 行同时更新 readme_sha
    （避免次日 ensure 把手动写入的 sha 当作变更误触发重生），week/quarter 行 readme_sha 保持 NULL。
    周/季增量语境按当前页期次换算（历史周页重生即该周窗口口径，出席才带增量行）；
    repo 不在跟踪池 → 404；AI/网络失败 → 502（旧文本保留，前端 toast"推荐语生成失败，稍后再试"）；
    DeepSeekAuthError（key 未配置/无效/余额）→ 500 带修复指引（§8.3 前端 toast"未配置 DeepSeek API key"）。
    """
    full_name, dimension, period_label = await _parse_recommend_payload(request)
    now = datetime.now(timezone.utc)
    conn = get_conn()
    try:
        repo = conn.execute(
            "SELECT id, description_en, description_zh, language FROM repos WHERE full_name = ?", (full_name,)
        ).fetchone()
        if repo is None:
            raise HTTPException(status_code=404, detail=f"仓库不在跟踪池：{full_name}")
        # README 输入（失败/空退化，绝不抛出阻塞）；stats 传占位 dict：单条不计 readme_fetched 计数
        readme_state = _ReadmeState(github, logger)
        readme_text, readme_sha = await readme_state.get(full_name, {"readme_fetched": 0})
        delta = stars = None
        if dimension != "total":
            as_of = _as_of_for_label(dimension, period_label, now)
            info = compute_repo_deltas(conn, period=dimension, as_of=as_of, repo_ids=[repo["id"]]).get(repo["id"])
            if info is not None:
                delta, stars = info.delta, info.stars  # 缺席（dead/首周）→ None：prompt 不写增星/总星行
        try:
            text = await client.recommend(
                full_name=full_name,
                description=repo["description_zh"] or repo["description_en"] or "（无简介）",
                language=repo["language"] or "未知",
                delta=delta,
                stars=stars,
                categories=[],
                dimension=dimension,
                readme=readme_text,
            )
        except Exception as exc:  # 不写库：失败旧文本保留（§8.3）；HTTP 映射见 _recommend_error_to_http
            raise _recommend_error_to_http(exc) from None
        week = _week_label(now.date())
        if dimension == "total":
            # F2-1 修复：本次 README 拉取失败（sha=None）不清指纹——保留旧值，防次日 ensure
            # 把手动重生的行误判为 README 变更再触发重生（拉取成功才更新 sha）
            prev_sha = conn.execute(
                "SELECT readme_sha FROM recommendations"
                " WHERE repo_id = ? AND dimension = 'total' AND period_label = 'all'",
                (repo["id"],),
            ).fetchone()
            stored_sha = readme_sha if readme_sha is not None else (prev_sha["readme_sha"] if prev_sha else None)
            conn.execute(
                "INSERT OR REPLACE INTO recommendations (repo_id, dimension, period_label, text, readme_sha,"
                " generated_week) VALUES (?, 'total', 'all', ?, ?, ?)",
                (repo["id"], text, stored_sha, week),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO recommendations (repo_id, dimension, period_label, text, readme_sha,"
                " generated_week) VALUES (?, ?, ?, ?, NULL, ?)",
                (repo["id"], dimension, period_label, text, week),
            )
        conn.commit()
        return {
            "full_name": full_name,
            "recommended": True,
            "text": text,
            "dimension": dimension,
            "period_label": period_label,
            "reason_label": _reason_label(dimension, period_label),  # 前端局部替换块标题同构（免 JS 重复映射）
        }
    finally:
        conn.close()


def _recommend_missing_count(conn: sqlite3.Connection) -> int:
    """批量补缺 total：S × 适用维度中 (repo_id, dimension, period_label) 行缺失数。

    S = 三口径榜（主榜 Top50 ∪ 新崛起区 Top10 ∪ 新项目区 Top20）去重 ∪ 关注集（_scope_sets 同源）；
    纯 DB 判定（不含 README sha 变更重生——那是每日 ensure 自动口径；批量 refresh=False 只补缺失），
    与 worker 实际补缺判定一致，保证进度 X ≤ T 恒成立。
    """
    now = datetime.now(timezone.utc)
    listed_by_period, follow_names = _scope_sets(conn, now=now)
    periods = [("week", _week_label(now.date())), ("quarter", _quarter_label(now.date())), ("total", "all")]
    existing = {
        (r["repo_id"], r["dimension"], r["period_label"])
        for r in conn.execute("SELECT repo_id, dimension, period_label FROM recommendations")
    }
    all_names = list(follow_names)
    for period in ("week", "quarter", "total"):
        for name in listed_by_period[period]:
            if name not in all_names:
                all_names.append(name)
    id_by_name: dict[str, int] = {}
    for offset in range(0, len(all_names), _NAME_LOOKUP_CHUNK):
        chunk = all_names[offset : offset + _NAME_LOOKUP_CHUNK]
        placeholders = ", ".join("?" * len(chunk))
        for r in conn.execute(f"SELECT id, full_name FROM repos WHERE full_name IN ({placeholders})", chunk):
            id_by_name[r["full_name"]] = r["id"]
    total = 0
    for period, label in periods:
        names = list(follow_names) if period == "total" else list(listed_by_period[period])
        if period == "total":
            for name in listed_by_period["total"]:
                if name not in names:
                    names.append(name)
        for full_name in names:
            rid = id_by_name.get(full_name)
            if rid is not None and (rid, period, label) not in existing:
                total += 1
    return total


@router.post("/api/recommend-missing", status_code=202)
async def api_recommend_missing() -> dict:
    """批量补齐推荐语（§8.2 第 2 步，后台任务形态；与翻译批量并列独立不共用）：锁内原子检查＋占位
    running → 统计补缺 total（S = 三口径榜 Top50 去重 ∪ 关注集 × 适用维度行缺失）→
    asyncio.create_task 起 worker（模块级持有防 GC）→ 立即返回 202；running 时重复触发 409
    （detail 即 §8.3 前端 toast 文案）；不触碰翻译字段。进度/结果由 worker 写入 _rec_batch_state。
    """
    global _rec_batch_task
    conn = get_conn()
    try:
        with _rec_batch_state_lock:
            if _rec_batch_state["running"]:
                raise HTTPException(status_code=409, detail="补齐推荐语任务进行中，请稍后再试")
            total = _recommend_missing_count(conn)
            _rec_batch_state.update(running=True, recommended=0, failed=0, total=total, finished=False, error=None)
    finally:
        conn.close()
    _rec_batch_task = asyncio.create_task(_run_batch_recommend())
    return {"started": True, "total": total}


async def _run_batch_recommend() -> None:
    """后台批量补齐推荐语 worker（§8.2/§8.4）：只补范围内缺失（S × 适用维度，不强制重生；
    README sha 变更重生归每日 ensure 自动口径）；自开 DB 连接与 AI/GitHub client（不依赖请求生命周期，
    finally 双双关闭）；逐条生成沿用 ensure 推荐段同口径——README 失败退化输入、单条失败记 WARNING
    计入 failed 继续整批、每条写库＋commit 立即落库、进度经 on_progress 渐进更新 state（前端"已补 X/T 条"）；
    DeepSeekAuthError（key 未配置/无效/余额）→ error="auth" 终止整批（已生成保留）；
    其余未知异常 → logger.exception＋error="unknown"；结束一律 running=False、finished=True
    （state 保留供查询；下次 POST 时重置计数与 error）。
    """
    conn = ai_client = github_client = None
    try:
        conn = get_conn()
        ai_client = _make_ai_client()
        github_client = _make_github_client()

        def _progress(stats: dict) -> None:
            with _rec_batch_state_lock:
                _rec_batch_state["recommended"] = stats["recommended"]

        stats = await recommend_missing(
            conn,
            ai_client,
            now=datetime.now(timezone.utc),
            log=logger,
            github_client=github_client,
            refresh=False,
            on_progress=_progress,
        )
        with _rec_batch_state_lock:
            _rec_batch_state["failed"] = stats["recommend_failed"]
    except DeepSeekAuthError as exc:
        logger.warning("AI 批量补齐推荐语终止（账户类错误）：%s", exc)
        with _rec_batch_state_lock:
            _rec_batch_state["error"] = "auth"
        return
    except Exception:
        logger.exception("AI 批量补齐推荐语异常终止")
        with _rec_batch_state_lock:
            _rec_batch_state["error"] = "unknown"
    finally:
        for obj in (ai_client, github_client):
            if obj is not None:
                try:
                    await obj.aclose()
                except Exception:  # 清理失败不阻断终端状态落盘（防假 client 无 aclose 等）
                    logger.exception("批量推荐 client 关闭失败")
        if conn is not None:
            conn.close()
        with _rec_batch_state_lock:
            _rec_batch_state["running"] = False
            _rec_batch_state["finished"] = True


@router.get("/api/recommend-missing/status")
async def api_recommend_missing_status() -> dict:
    """批量补齐推荐语进度查询（§8.2）：前端每 2s 轮询；不依赖 AI/GitHub client，无任务史时全零/false/None。"""
    with _rec_batch_state_lock:
        return dict(_rec_batch_state)


# ===== 手动同步 API（T-029；口径《交互流程说明》§14.4 钉死：同步＝daily_job 全链路，与调度器共用一把运行锁） =====


@router.post("/api/sync", status_code=202)
async def api_sync() -> dict:
    """手动同步（§14.2 第 1 步，后台任务形态）：try_start_sync 原子抢占运行锁（锁与状态在 app.jobs，
    调度器与手动共用，§14.4 全局限额一个运行实例；抢锁与起任务在 jobs 内部成对完成，本层只判返回值
    防双跑）→ 202 {"started": True}；running 时重复触发 409（detail 即 §14.3 前端 toast 文案
    "同步进行中…"）。同步＝daily_job 全链路（快照→发现→榜单预计算→AI ensure），与调度器走同一条
    代码路径，不另实现一套；进度/结果由 jobs 写入 _sync_state，前端轮询 /status 读取。"""
    if not try_start_sync():
        raise HTTPException(status_code=409, detail="同步进行中，请稍后再试")
    return {"started": True}


@router.get("/api/sync/status")
async def api_sync_status() -> dict:
    """手动同步状态查询（§14.2 第 2 步）：前端每 2s 轮询；不依赖外部 API，无任务史（服务刚重启/
    从未跑过）时 running=False 且各字段为 None。"""
    return sync_status()
