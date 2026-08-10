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
- 本层对 recommendations 表只读展示（T-011 才接线生成）。
"""

from __future__ import annotations

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

from app.ai import DeepSeekAuthError, DeepSeekClient, has_cjk
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
from app.report import Board, ReportRow, compute_boards, compute_repo_deltas

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
    """date → ISO 周标签（%G-W%V 定宽零填充，字符串比较即时间序）。"""
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _quarter_label(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


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


def _meta(conn: sqlite3.Connection, period: str, as_of_date: date) -> dict:
    """元信息行（流程说明 §2 第 1 层）：统计窗口、生成时间（UTC+8）、跟踪池规模、口径说明。"""
    nominal = _NOMINAL_DAYS.get(period)
    window = None
    if nominal is not None:
        start = as_of_date - timedelta(days=nominal)
        window = f"{start.isoformat()} → {as_of_date.isoformat()}（{nominal} 天）"
    tracked = conn.execute("SELECT COUNT(*) FROM repos WHERE dead = 0").fetchone()[0]
    beijing = timezone(timedelta(hours=8))  # 本人自用（中国时区），明示避免与库内 UTC 混淆
    return {
        "window": window,  # 总星榜无窗口概念 → None，模板改显"截至"
        "as_of": as_of_date.isoformat(),
        "generated": datetime.now(beijing).strftime("%Y-%m-%d %H:%M"),
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


def _endpoint_stars(conn: sqlite3.Connection, repo_id: int, before: str) -> int | None:
    """dead 关注仓库的端点星数：与 app.report._ENDPOINT_SQL 同一主键索引 seek 模式（每仓库一次 LIMIT 1）。"""
    row = conn.execute(
        "SELECT stars FROM star_snapshots WHERE repo_id = ? AND captured_at <= ? ORDER BY captured_at DESC LIMIT 1",
        (repo_id, before),
    ).fetchone()
    return None if row is None else row["stars"]


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
        stars = info.stars if info is not None else _endpoint_stars(conn, r["id"], as_of)
        delta = info.delta if info is not None else None
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
                "window_note": window_note,
            }
        )
    return cards


def _follow_groups(cards: list[dict], reasons: dict, zh: dict, tags: dict) -> list[dict]:
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
                    "tags": tags.get(r["full_name"], []),
                    "window_note": r["window_note"],
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


def _display_maps(conn: sqlite3.Connection, as_of_date: date) -> tuple[dict, dict, dict]:
    """详情面板展示映射（recommendations / description_zh / tags 三张表全部只读）：
    - 推荐理由 recommendations：按 as_of 所在 ISO 周取（周报页即该周；季/总星页取 as_of 当周，T-011 接线时可再调）；
    - 中文描述 description_zh：懒写入，当前全 NULL → 只显示英文（AI 降级口径，流程说明 §4）；
    - 标签 tags：每行 chips 数据源（行内增删走 /api/tags、结果页跳转 /tags/<tag>，T-010 接线）。
    """
    week_key = _week_label(as_of_date)
    reasons = dict(
        conn.execute(
            "SELECT r.full_name, c.text FROM recommendations c JOIN repos r ON r.id = c.repo_id WHERE c.report_week = ?",
            (week_key,),
        ).fetchall()
    )
    zh = dict(conn.execute("SELECT full_name, description_zh FROM repos WHERE description_zh IS NOT NULL").fetchall())
    tags: dict[str, list[str]] = {}
    for row in conn.execute("SELECT r.full_name, t.tag FROM tags t JOIN repos r ON r.id = t.repo_id ORDER BY t.tag"):
        tags.setdefault(row["full_name"], []).append(row["tag"])
    return reasons, zh, tags


def _row_view(rank: int, row: ReportRow, *, period: str, followed: set, reasons: dict, zh: dict, tags: dict) -> dict:
    """榜单行 + 详情面板的模板视图：模板只负责渲染，一切格式化在本层完成。"""
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
        "tags": tags.get(row.full_name, []),
        "window_note": window_note,
    }


def _board_view(board: Board, **row_ctx) -> dict:
    return {
        # kind 必须入锚点：language 榜的 other 与 topic 榜的 other 撞 key
        "anchor": f"b-{board.kind}-{board.key}",
        "label": board.label,
        "rows": [_row_view(i + 1, r, **row_ctx) for i, r in enumerate(board.rows)],
    }


def _boards_context(
    request: Request,
    *,
    period: str,
    label: str,
    as_of: str,
    as_of_date: date,
    now: datetime,
) -> dict:
    """三页面共用的上下文装配：榜单（含首期空态降级）→ 元信息 → 期次控件。

    DB 连接请求级获取/关闭（不持全局长连接）；WAL 下读榜单不阻塞每日采集写入。
    """
    conn = get_conn()
    try:
        boards = compute_boards(conn, _topic_table(), period=period, as_of=as_of, top_n=30)
        notice = None
        effective_period = period
        if period in _NOMINAL_DAYS and all(not b.rows for b in boards):
            # 首期空态降级（流程说明 §4）：增量榜全缺席 → 显示总星榜 + 顶部提示条
            boards = compute_boards(conn, _topic_table(), period="total", as_of=as_of, top_n=30)
            effective_period = "total"
            notice = _fallback_notice(conn, period, label, as_of_date, any(b.rows for b in boards))

        # 关注集只取两处展示用途：行内星标 on/off 态、顶栏"我的关注"计数徽标（v1.3 起关注区在 P6 独立页）
        follow_rows = conn.execute("SELECT r.full_name FROM follows f JOIN repos r ON r.id = f.repo_id").fetchall()
        followed = {r["full_name"] for r in follow_rows}

        reasons, zh, tags = _display_maps(conn, as_of_date)
        row_ctx = {"period": effective_period, "followed": followed, "reasons": reasons, "zh": zh, "tags": tags}
        lang_boards = [_board_view(b, **row_ctx) for b in boards if b.kind == "language"]
        topic_boards = [_board_view(b, **row_ctx) for b in boards if b.kind == "topic"]

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
            "meta": _meta(conn, period, as_of_date),  # 窗口/口径按请求期次展示，不因降级改写成总星榜口径
            "notice": notice,
            "follow_count": len(follow_rows),
            "lang_boards": lang_boards,
            "topic_boards": topic_boards,
        }
    finally:
        conn.close()


# ===== 路由 =====


@router.get("/", response_class=HTMLResponse)
def weekly(request: Request, week: str | None = None) -> HTMLResponse:
    """P1 本周报告（=最新一期）；?week=2026-W32 回看历史周次（P2 与 P1 同页换期次，流程说明 §1）。"""
    now = datetime.now(timezone.utc)
    label, as_of, as_of_date = _resolve_week(week, now)
    return templates.TemplateResponse(
        request=request,
        name="boards.html",
        context=_boards_context(request, period="week", label=label, as_of=as_of, as_of_date=as_of_date, now=now),
    )


@router.get("/quarter", response_class=HTMLResponse)
def quarterly(request: Request, quarter: str | None = None) -> HTMLResponse:
    """P3 季度回顾（90 天增量榜）；?quarter=2026-Q3 回看往期。"""
    now = datetime.now(timezone.utc)
    label, as_of, as_of_date = _resolve_quarter(quarter, now)
    return templates.TemplateResponse(
        request=request,
        name="boards.html",
        context=_boards_context(request, period="quarter", label=label, as_of=as_of, as_of_date=as_of_date, now=now),
    )


@router.get("/total", response_class=HTMLResponse)
def total(request: Request) -> HTMLResponse:
    """P4 总星榜：最新快照总星数降序 Top 30/榜，无期次概念。"""
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
        ),
    )


@router.get("/follows", response_class=HTMLResponse)
def follows_page(request: Request) -> HTMLResponse:
    """P6 我的关注（v1.3 独立页）：关注仓库按语言分组，增量与当期周报同窗口（as_of=now 周口径）。

    无期次概念（不跟周次切换走）；空关注时渲染空态引导（流程说明 §4），分组/排序口径在 _follow_groups。
    """
    now = datetime.now(timezone.utc)
    conn = get_conn()
    try:
        follow_rows = conn.execute(
            "SELECT r.id, r.full_name, r.language, r.description_en, r.dead"
            " FROM follows f JOIN repos r ON r.id = f.repo_id ORDER BY f.created_at"
        ).fetchall()
        cards = _follow_cards(conn, follow_rows, now.strftime(_ISO_FMT))
        reasons, zh, tags = _display_maps(conn, now.date())
        return templates.TemplateResponse(
            request=request,
            name="follows.html",
            context={
                "request": request,
                "page": "follows",  # 顶栏 active 态
                "title": "我的关注",
                "meta": _meta(conn, "week", now.date()),  # 与当期周报同窗口口径（v1.3 §2A 第 1 层）
                "follow_count": len(follow_rows),
                "groups": _follow_groups(cards, reasons, zh, tags),
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


def _tag_row_view(rank: int, row: sqlite3.Row, stars: int | None, *, followed: set, reasons: dict, zh: dict, tags: dict) -> dict:
    """标签结果页行视图：与 _row_view 同字段契约（_row.html 共用），无增量列（按总星排序口径）。

    dead 仓库保留展示（打过的标签仍在）：整行灰显＋"已失效"（_row.html 既有分支），up 列退化"——"。
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
        "tags": tags.get(row["full_name"], []),
        "window_note": None,
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
            "SELECT r.id, r.full_name, r.language, r.description_en, r.dead"
            " FROM tags t JOIN repos r ON r.id = t.repo_id WHERE t.tag = ?",
            (tag,),
        ).fetchall()
        starred = []
        for r in rows:
            starred.append((_endpoint_stars(conn, r["id"], as_of), r))
        starred.sort(key=lambda t: (-(t[0] or 0), t[1]["full_name"]))  # 无快照行按 0 沉底，同星按名稳定

        reasons, zh, tags = _display_maps(conn, now.date())
        follow_rows = conn.execute("SELECT r.full_name FROM follows f JOIN repos r ON r.id = f.repo_id").fetchall()
        followed = {r["full_name"] for r in follow_rows}
        view_rows = [
            _tag_row_view(i + 1, row, stars, followed=followed, reasons=reasons, zh=zh, tags=tags)
            for i, (stars, row) in enumerate(starred)
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
            },
        )
    finally:
        conn.close()


# ===== 翻译 API（T-016；口径《交互流程说明》§7.4 钉死：单个强制重译 / 批量只补 NULL，不触碰 recommendations 表） =====

_batch_translating_lock = threading.Lock()  # in-flight 锁：批量补译防重入（§7.3 409）；S 档单进程跨请求共享


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


@router.post("/api/translate-missing")
async def api_translate_missing(client: DeepSeekClient = Depends(_ai_client)) -> dict:
    """批量补译（§7.2 第 2 步）：全池 description_zh IS NULL 且英文非空无 CJK（含 dead）只补 NULL 不覆盖；
    in-flight 锁防重入——进行中重复触发 409（§7.3）；不触碰 recommendations 表。

    降级口径（与 T-011 ensure 同链）：单条失败记 WARNING 跳过计入 failed，不中断整批（次日/再触发自然补缺）；
    DeepSeekAuthError（key 未配置/无效/余额）是确定性配置错误 → 直通 500，前端 toast"未配置 DeepSeek API key"。
    每批整体响应 200 后前端 toast"补译完成：新译 N 条"。
    """
    if not _batch_translating_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="补译任务进行中，请稍后再试")
    conn = get_conn()
    try:
        pending = conn.execute(
            "SELECT id, full_name, description_en FROM repos WHERE description_zh IS NULL"
        ).fetchall()
        translated = 0
        failed = 0
        for row in pending:
            text_en = row["description_en"]
            if not text_en or not text_en.strip():
                continue  # GitHub 官方允许无简介：空描述跳过
            if has_cjk(text_en):
                continue  # 原文已含中文（含中英混排），不送译
            try:
                zh = await client.translate(text_en)
            except DeepSeekAuthError as exc:
                # 账户类确定性错误（key 空/无效/余额）：直通整轮 handler——detail 带修复指引，
                # 前端按非 200 统一 toast"未配置 DeepSeek API key"（§7.3）
                raise HTTPException(status_code=500, detail=str(exc)) from None
            except Exception as exc:
                logger.warning("AI 批量补译失败，跳过 %s：%s", row["full_name"], exc)
                failed += 1
                continue
            conn.execute("UPDATE repos SET description_zh = ? WHERE id = ?", (zh, row["id"]))
            conn.commit()
            translated += 1
        return {"translated": translated, "failed": failed}
    finally:
        conn.close()
        _batch_translating_lock.release()
