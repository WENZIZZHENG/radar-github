"""T-008 榜单页面（SSR）：本周报告（含历史周次切换）/ 季度回顾 / 总星榜。

职责边界（任务书钉死，越界打回）：
- 榜单口径全部委托 app.report.compute_boards / compute_repo_deltas（《架构决策记录》决策 3/4 唯一实现），本层不重算；
- 本层只做：URL 期次参数 → as_of 换算、首期空态降级（《交互流程说明》§4）、展示格式化（数字/颜色/锚点）；
- 写操作一律不在本层：关注 T-009、标签增删与结果页 T-010、推荐理由生成 T-011；
  本层对 follows / recommendations / tags 三张表只读展示。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.classify import load_topics
from app.config import BASE_DIR
from app.db import get_conn
from app.report import Board, ReportRow, compute_boards, compute_repo_deltas

_WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = _WEB_DIR / "static"  # 供 app.main 挂载 StaticFiles（/static）
TOPICS_PATH = BASE_DIR / "config" / "topics.yaml"

templates = Jinja2Templates(directory=_WEB_DIR / "templates")
router = APIRouter()

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
    """我的关注（流程说明 §2 第 2 层）：增量取周口径 deltas；dead 仓库被 compute_repo_deltas 剔除，端点星数单独补取。"""
    if not follow_rows:
        return []
    deltas = compute_repo_deltas(conn, period="week", as_of=as_of)
    cards = []
    for r in follow_rows:
        info = deltas.get(r["id"])
        stars = info.stars if info is not None else _endpoint_stars(conn, r["id"], as_of)
        delta_text = _fmt_delta(info.delta) if info is not None and info.delta is not None else None
        cards.append(
            {
                "full_name": r["full_name"],
                "dead": bool(r["dead"]),
                "delta_text": delta_text,  # None → 首周缺席口径：模板渲染"—— 下周起有数据"
                "stars_text": None if stars is None else _fmt_stars(stars),
            }
        )
    return cards


def _display_maps(conn: sqlite3.Connection, as_of_date: date) -> tuple[dict, dict, dict]:
    """详情面板展示映射（三张表全部只读）：
    - 推荐理由 recommendations：按 as_of 所在 ISO 周取（周报页即该周；季/总星页取 as_of 当周，T-011 接线时可再调）；
    - 中文描述 description_zh：懒写入，当前全 NULL → 只显示英文（AI 降级口径，流程说明 §4）；
    - 标签 tags：只读 chips 展示；增删与标签结果页跳转归 T-010。
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
    show_follows: bool,
) -> dict:
    """三页面共用的上下文装配：榜单（含首期空态降级）→ 关注区 → 元信息 → 期次控件。

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

        follow_rows = conn.execute(
            "SELECT r.id, r.full_name, r.dead FROM follows f JOIN repos r ON r.id = f.repo_id ORDER BY f.created_at"
        ).fetchall()
        followed = {r["full_name"] for r in follow_rows}
        # 关注区置顶仅属周报页（流程说明 §2 五层是 P1 结构；P3/P4"布局同 P1 榜单区"不含关注区）
        follows = _follow_cards(conn, follow_rows, as_of) if show_follows else None

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
            "follows": follows,
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
        context=_boards_context(
            request, period="week", label=label, as_of=as_of, as_of_date=as_of_date, now=now, show_follows=True
        ),
    )


@router.get("/quarter", response_class=HTMLResponse)
def quarterly(request: Request, quarter: str | None = None) -> HTMLResponse:
    """P3 季度回顾（90 天增量榜）；?quarter=2026-Q3 回看往期。"""
    now = datetime.now(timezone.utc)
    label, as_of, as_of_date = _resolve_quarter(quarter, now)
    return templates.TemplateResponse(
        request=request,
        name="boards.html",
        context=_boards_context(
            request, period="quarter", label=label, as_of=as_of, as_of_date=as_of_date, now=now, show_follows=False
        ),
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
            show_follows=False,
        ),
    )
