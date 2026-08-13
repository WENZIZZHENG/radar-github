"""T-008 榜单页面测试：TestClient 打真实 app（lifespan 会跑 init_db），tmp_path 独立库（RADAR_DB_PATH 覆盖）+ 调度关闭。

断言分两类：
- 结构断言（时间稳健）：三页面 200、榜单区块数（T-026 起默认单榜 1 块、?board=all 17 块、降级页全量 17 块）、
  P1/P2/P3/P4 边栏 18 项（17 榜项 + 1 "全部"项，T-026 §13.1 起 P4 纳入边栏替代 chips）、
  默认展开态、空态降级提示、期次参数 200/400；
  周/季出席数据随"今天"推移会滑出窗口，故不做数据内容断言（口径语义归 tests/test_report.py）。
- 内容断言全走 /total?board=all（最新快照降序，无窗口概念，时间稳健）与首期空态（快照仅同一天）；
  例外：历史期次页（/?week=2026-W32&board=all、/quarter?quarter=2026-Q3&board=all）as_of 固定、
  种子快照恒出席，允许内容断言（T-017 复验打回修复起；T-026 单榜后加 board=all 恢复全量语境）。
"""

import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn, init_db
from app.main import app

# 构造数据挂在固定的历史日期（2026-08-09 = 2026-W32 周日，也是真实库首日）：
# 该周永为过去/当前周，期次参数断言不依赖"今天"是哪天
SNAP_DAY = "2026-08-09"
WEEK_LABEL = "2026-W32"
PREV_WEEK_LABEL = "2026-W31"


def _add_repo(conn, name, *, language=None, topics=(), description_en=None, description_zh=None, snapshots=()):
    """插一个仓库及其快照，返回 repo_id；created_at 取最早快照日（与真实采集一致：先入池后有快照）。"""
    created_at = min((ts for ts, _ in snapshots), default=SNAP_DAY)
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, 'test', ?)",
        (name, f"node-{name}", description_en, description_zh, language, json.dumps(list(topics)), created_at),
    )
    repo_id = cur.lastrowid
    for captured_at, stars in snapshots:
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, captured_at, stars),
        )
    return repo_id


def _seed_full(conn):
    """三口径齐数据的库：快照两端跨 90 天；附关注/推荐语/中文描述/标签各一条，覆盖详情面板全部展示路径。"""
    repo_py = _add_repo(
        conn,
        "a/py",
        language="Python",
        topics=["ai"],
        description_en="Python lib",
        description_zh="Python 库",
        snapshots=[("2026-05-11T00:00:00Z", 100), ("2026-08-02T00:00:00Z", 300), (f"{SNAP_DAY}T00:00:00Z", 400)],
    )
    repo_go = _add_repo(
        conn,
        "a/go",
        language="Go",
        description_en="Go lib",
        snapshots=[("2026-05-11T00:00:00Z", 50), ("2026-08-02T00:00:00Z", 150), (f"{SNAP_DAY}T00:00:00Z", 200)],
    )
    # T-018 新区种子：两端快照跨度 3 天（<5 缺席）→ 历史周/季页新区行（入池 +300、3 天，时间稳健）
    _add_repo(
        conn,
        "a/rise",
        language="Rust",
        topics=["cli"],
        description_en="rising star",
        snapshots=[("2026-08-06T00:00:00Z", 100), (f"{SNAP_DAY}T00:00:00Z", 400)],
    )
    conn.execute("INSERT INTO follows (repo_id, created_at) VALUES (?, ?)", (repo_go, "2026-08-02T00:00:00Z"))
    # T-017 推荐语按 (dimension, period_label) 展示：总星行文本供 /total（时间稳健）断言；
    # 周/季行按固定期次标签插入（2026-W32/2026-Q3），供历史周/季页时间稳健断言
    # T-024 概要行：dimension='summary' 且 period_label='all'（全页面同一条，文档视角）；
    # a/py 与 a/go 各一条（关注页走 a/go 的概要断言），a/rise 无概要行（降级断言）
    conn.execute(
        "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
        " VALUES (?, 'total', 'all', '本周亮点：测试推荐语', NULL, '2026-W32'),"
        " (?, 'week', '2026-W32', '周报亮点：周推荐语', NULL, '2026-W32'),"
        " (?, 'quarter', '2026-Q3', '季报亮点：季推荐语', NULL, '2026-W32'),"
        " (?, 'summary', 'all', '概要文本：Python 库', NULL, '2026-W32'),"
        " (?, 'summary', 'all', '概要文本：Go 库', NULL, '2026-W32')",
        (repo_py, repo_py, repo_py, repo_py, repo_go),
    )
    conn.execute("INSERT INTO tags (repo_id, tag) VALUES (?, ?)", (repo_py, "选型观察"))


def _seed_first_day(conn):
    """首期状态（与当前真实库同形）：快照仅同一天 → 周/季榜全缺席、总星榜有数据。"""
    _add_repo(
        conn,
        "a/new",
        language="Rust",
        topics=["cli"],
        description_en="new hotness",
        snapshots=[(f"{SNAP_DAY}T05:51:43Z", 1234)],
    )


def _make_client(tmp_path, monkeypatch, seed):
    db = tmp_path / "web.db"
    init_db(db)
    conn = get_conn(db)
    seed(conn)
    conn.commit()
    conn.close()
    # get_settings 每次调 getenv 无缓存，RADAR_DB_PATH 请求级生效；调度关闭（tests/test_smoke.py 同口径）
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    return TestClient(app)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, _seed_full) as c:
        yield c


@pytest.fixture()
def fresh_client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, _seed_first_day) as c:
        yield c


def test_three_pages_ok(client):
    for path in ("/", "/quarter", "/total"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "GitHub 雷达" in resp.text


def test_index_has_17_boards_and_sidebar(client):
    """T-021：17 榜齐备（语言 7 + 主题 10），空榜也渲染板块；左侧边栏 17 榜项一一对应（原顶部 chips 已由边栏替代）。

    T-026 §13.1：无 board 参数 → 默认单榜（默认首榜 language-java，_seed_full 无 Java 仓 → 空态），
    只渲染 1 个榜块；board=all 才全量 17 榜块。边栏恒为 18 项 = 17 榜项（class="sb-item"，与既有计数
    兼容）+ 1 "全部"项（class="sb-item sb-all" 独立 class 防混淆）。
    """
    text = client.get("/").text
    assert text.count('class="board"') == 1  # 默认单榜：只渲染当前榜块（class="boards" 容器不带右引号，不会被误计）
    assert 'id="b-language-java"' in text  # 默认首榜 = 语言组第一榜
    assert text.count('id="b-language-') == 1 and text.count('id="b-topic-') == 0  # 单榜：语言榜块 1、主题榜块 0
    assert "本期暂无数据" in text  # _seed_full 无 Java 仓 → 当前榜空态文案
    assert '<aside class="sidebar"' in text
    assert _sb_item_count(text) == 17  # 边栏榜项 = 语言 7 + 主题 10（"全部"项是 sb-all 独立 class）
    assert _sb_all_count(text) == 1  # "全部"项（board=all 全量入口，§13.1）
    assert text.count('<a class="chip" href="#b-') == 0  # 顶部 chips 区不再渲染（边栏替代）
    # board=all 全量 17 榜块（保留全量入口）
    text_all = client.get("/?board=all").text
    assert text_all.count('class="board"') == 17
    assert _sb_item_count(text_all) == 17 and _sb_all_count(text_all) == 1
    assert 'class="sb-item sb-all active"' in text_all  # 全量模式当前为"全部"项（服务端渲染 active）


def test_first_week_page_keeps_boards_when_rising_has_rows(fresh_client):
    """首期降级判定变更（T-018）：主榜全空但新区有行 → 不降级（无 notice 无总星榜降级）；
    主榜区显示既有空态"本期暂无数据"＋新区小节照常渲染；17 板块仍在、边栏仍 17 项。

    T-026 适配：board=all 显式全量语境断言（默认单榜只渲染当前榜块，17 板块的全量断言归 board=all；
    降级判定本身在单榜模式同样成立——?board=language-java 时全库新区有行 → 不降级，见新增测试）。"""
    resp = fresh_client.get("/?board=all")
    assert resp.status_code == 200
    assert 'class="notice"' not in resp.text  # 新区有行 → 不降级
    assert "初始总星榜" not in resp.text  # 不降级到总星榜
    assert "a/new" in resp.text  # 新区行确实渲染
    assert "新崛起 · 入池未满一个统计窗口" in resp.text
    assert resp.text.count("本期暂无数据") == 17  # 主榜全空：每榜空态照常
    assert resp.text.count('class="board"') == 17
    assert _sb_item_count(resp.text) == 17  # T-021：顶部 chips 已由左侧边栏替代（17 项）
    assert "follows-sec" not in resp.text  # v1.3：P1 顶部关注区已移出为独立页 P6
    # 默认全部展开（D1/D3 v1.1）：行即展开态、面板 open、aria 同步
    assert 'class="row expanded"' in resp.text
    assert 'class="panel open"' in resp.text
    assert 'aria-expanded="true"' in resp.text


def test_history_week_param(fresh_client):
    """历史周次参数：合法周 200 且页内显示该周次；快照首日所在周主榜空但新区有行 → 不降级；
    跟踪池建立前的周次主榜新区全空 → 降级无数据提示。

    T-026 适配：a/new 属 Rust 语言＋devtools 主题，默认单榜（language-java）不渲染它——
    指定 board=topic-devtools（其新区所在榜）做内容断言；board 参数与 week= 叠加生效（§13.1）。"""
    resp = fresh_client.get(f"/?week={WEEK_LABEL}&board=topic-devtools")
    assert resp.status_code == 200
    assert WEEK_LABEL in resp.text  # 期次控件当前期标签
    # T-018：a/new 单张快照 → 新区有行 → 不再降级（as_of 恒 2026-08-09 期末，时间稳健）
    assert "新崛起 · 入池未满一个统计窗口" in resp.text
    assert "a/new" in resp.text
    assert "初始总星榜" not in resp.text
    assert "首份周报预计将于" not in resp.text
    resp = fresh_client.get(f"/?week={PREV_WEEK_LABEL}")
    assert resp.status_code == 200
    assert "暂无可展示数据" in resp.text  # 2026-08-02 之前无任何快照：主榜新区全空 → 降级（降级页全量现状）


def test_week_param_invalid_and_future(client):
    """非法格式与不存在的周次 400（fail-loud，不静默回退本周）；未来周次 400。"""
    assert client.get("/?week=oops").status_code == 400
    assert client.get("/?week=2026-W99").status_code == 400
    nxt = date.today() + timedelta(days=7)
    iso = nxt.isocalendar()
    assert client.get(f"/?week={iso.year}-W{iso.week:02d}").status_code == 400


def test_quarter_param(client):
    """季度页：默认当期 200；合法季度参数 200；非法与未来季度 400。"""
    today = date.today()
    qlabel = f"{today.year}-Q{(today.month - 1) // 3 + 1}"
    resp = client.get("/quarter")
    assert resp.status_code == 200
    assert qlabel in resp.text  # 期次控件当前季标签
    assert client.get(f"/quarter?quarter={qlabel}").status_code == 200
    assert client.get("/quarter?quarter=bad").status_code == 400
    assert client.get("/quarter?quarter=2026-Q0").status_code == 400
    cur_q = (today.month - 1) // 3 + 1
    y, q = (today.year + 1, 1) if cur_q == 4 else (today.year, cur_q + 1)
    assert client.get(f"/quarter?quarter={y}-Q{q}").status_code == 400


def test_total_page_content(client):
    """总星榜（时间稳健的内容断言载体）：中文描述两行形态、推荐语块、标签只读 chip、已关注星标实心。

    T-026 适配：内容断言走 /total?board=all（全量语境——默认单榜只渲染 language-java 空榜）。"""
    resp = client.get("/total?board=all")
    assert resp.status_code == 200
    text = resp.text
    assert "a/py" in text and "a/go" in text
    assert "Python lib" in text and "Python 库" in text  # 有中文时中英都显示
    assert "本周亮点：测试推荐语" in text  # recommendations 左连展示
    assert "选型观察" in text  # tags 只读 chip（增删归 T-010）
    assert 'class="star on"' in text  # a/go 已关注 → 实心星
    assert "按最新快照总星数降序" in text  # 元信息行口径说明
    assert 'class="up"' not in text  # 总星榜无增量概念：不渲染增量列
    assert "follow-boards" not in text  # v1.3：关注分组区只在 P6 独立页渲染（P3/P4 布局同 P1 榜单区，不含关注区）


def test_follow_section_moved_off_weekly(client):
    """v1.3：P1 顶部关注区移出为独立页 P6；周报页不再有关注区，顶栏"我的关注"徽标显示真实关注数。"""
    text = client.get("/").text
    assert "follows-sec" not in text
    assert "fcard" not in text
    assert '我的关注<i id="nav-follow-count">1</i>' in text  # _seed_full 有一条关注（a/go）


def test_star_buttons_wired(client):
    """T-009 接线：星标启用（不再 disabled）、带 data-repo 与关注/取消关注 title；toast 容器就位。

    T-026 适配：行断言走 /total?board=all（a/py/a/go 不在默认首榜）。"""
    text = client.get("/total?board=all").text
    assert "关注功能开发中" not in text  # T-008 禁用态文案已移除
    assert "disabled" not in text  # 星标不再带 disabled 属性
    assert 'data-repo="a/py"' in text  # 未关注 → JS 据此 POST 关注
    assert 'class="star on" data-repo="a/go"' in text  # 已关注 → 实心＋取消关注态
    assert 'title="关注"' in text
    assert 'title="取消关注"' in text
    assert 'id="toasts"' in text  # toast 容器（关注/取消反馈）每页就位
    text = client.get("/").text
    assert '<a href="/follows"' in text  # P6 入口（v1.3 顶栏 5 项："我的关注"在"本周报告"之后）
    assert "follows-sec" not in text  # P1 关注区已移除，关注 API 不再向页面插卡（无 as_of 接线）


# ---------- T-015：P6 我的关注独立页 ----------

P6_NOW = datetime.now(timezone.utc)  # P6 增量口径 as_of=now（真实"今天"）：快照必须相对此刻造，否则滑出 5~9 天窗口


def _iso_ago(*, days=0, hours=0):
    """P6_NOW 之前指定偏移的定长 UTC 时间戳（与 schema 硬约定同格式）。"""
    return (P6_NOW - timedelta(days=days, hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_follows(conn):
    """P6 用例库：多语言关注集，覆盖正常增量/新入池 na/dead 三态与组内/组间排序。

    增量窗口取名义 7 天两端（start=P6_NOW-7d，end=P6_NOW-1h，跨度 ≈7 天落在 5~9 天接受区间）。
    """
    rows = [
        # (full_name, language, dead, snapshots)：TypeScript +500/+100，Python +300，Go 一 na 一 dead
        ("f/ts-hot", "TypeScript", 0, [(_iso_ago(days=7), 100), (_iso_ago(hours=1), 600)]),
        ("f/ts-mid", "TypeScript", 0, [(_iso_ago(days=7), 100), (_iso_ago(hours=1), 200)]),
        ("f/py-up", "Python", 0, [(_iso_ago(days=7), 200), (_iso_ago(hours=1), 500)]),
        ("f/go-new", "Go", 0, [(_iso_ago(hours=1), 66)]),  # 单张基线（首周缺席）→ na 行
        ("f/go-dead", "Go", 1, [(_iso_ago(days=7), 100), (_iso_ago(hours=1), 120)]),  # dead 剔除出增量
    ]
    for i, (name, lang, dead, snaps) in enumerate(rows):
        repo_id = _add_repo(conn, name, language=lang, description_en=f"{name} desc", snapshots=snaps)
        if dead:
            conn.execute("UPDATE repos SET dead = 1 WHERE id = ?", (repo_id,))
        conn.execute("INSERT INTO follows (repo_id, created_at) VALUES (?, ?)", (repo_id, f"2026-08-0{i + 1}T00:00:00Z"))
    conn.execute("INSERT INTO tags (repo_id, tag) VALUES ((SELECT id FROM repos WHERE full_name = 'f/ts-hot'), '选型观察')")
    # T-021 §12.2：未关注仓上的标签也进筛选 chips（0 计数，供空态演示）——不影响 P6 行集与关注计数
    repo_nf = _add_repo(
        conn, "f/nf", language="Rust", description_en="not followed",
        snapshots=[(_iso_ago(days=7), 10), (_iso_ago(hours=1), 12)],
    )
    conn.execute("INSERT INTO tags (repo_id, tag) VALUES (?, 'AI')", (repo_nf,))


@pytest.fixture()
def follows_client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, _seed_follows) as c:
        yield c


def test_follows_page_ok_and_nav(follows_client):
    """P6 200：元信息行（窗口口径＋关注总数）、顶栏"我的关注"active＋计数徽标 SSR 真值、行形态同榜单行。"""
    resp = follows_client.get("/follows")
    assert resp.status_code == 200
    text = resp.text
    assert "与当期周报同口径" in text  # 元信息行窗口口径（v1.3 §2A 第 1 层）
    assert 'id="follow-total">5</b>' in text  # 关注总数
    assert '<a href="/follows" class="active" aria-current="page">我的关注<i id="nav-follow-count">5</i></a>' in text
    assert 'class="row expanded"' in text and 'class="panel open"' in text  # 紧凑行＋详情默认展开
    assert text.count('class="star on"') == 5  # P6 行恒金色★ on（点击即取消）
    assert "f/ts-hot desc" in text  # 详情面板内容渲染
    assert "选型观察" in text  # 标签区只读 chips（增删归 T-010）


def test_follows_page_groups_and_empty_groups(follows_client):
    """分组：关注按语言各归其组（classify_language 同口径）；组块头"N 个关注"；空组不渲染。"""
    text = follows_client.get("/follows").text
    assert "<h3>TypeScript<span" in text and "<h3>Python<span" in text and "<h3>Go<span" in text
    assert text.count("2 个关注") == 2 and text.count("1 个关注") == 1  # TS/Go 各 2 行，Python 1 行
    for key in ("f-java", "f-rust", "f-javascript", "f-other"):
        assert f'id="{key}"' not in text  # 空组不渲染（v1.3 §2A）


def test_follows_page_group_order(follows_client):
    """组间按组内最大当周增量降序：TypeScript(+500) → Python(+300) → Go（仅 na/dead，按 0 沉后）。"""
    text = follows_client.get("/follows").text
    i_ts = text.index('id="f-typescript"')
    i_py = text.index('id="f-python"')
    i_go = text.index('id="f-go"')
    assert i_ts < i_py < i_go


def test_follows_page_row_order_within_group(follows_client):
    """组内三态排序：正常行按增量降序（+500 在 +100 前）→ 无增量行（na）→ dead 行沉组尾。"""
    text = follows_client.get("/follows").text
    assert text.index("f/ts-hot") < text.index("f/ts-mid")
    assert text.index("f/go-new") < text.index("f/go-dead")


def test_follows_page_na_and_dead_row_marks(follows_client):
    """三态标记：na 行灰字"—— 下周起有数据"；dead 行整行灰显＋"已失效"标；两行 up 列均为灰字 na 形态。"""
    text = follows_client.get("/follows").text
    assert "—— 下周起有数据" in text
    assert text.count('class="up na"') == 2  # na 行"—— 下周起有数据"＋ dead 行"——"
    assert 'class="row expanded dead"' in text  # dead 行灰显
    assert '<span class="dead-tag">已失效</span>' in text


def test_follows_page_empty_state(fresh_client):
    """空关注空态（v1.3 §4）：引导文案＋回首页链接；分组区不渲染；计数徽标 0。"""
    resp = fresh_client.get("/follows")
    assert resp.status_code == 200
    text = resp.text
    assert "还没有关注任何项目：去榜单点行右侧 ☆，该项目每周增量会出现在这里" in text
    assert '<a href="/">回首页</a>' in text
    assert 'id="follow-boards"' not in text
    assert '我的关注<i id="nav-follow-count">0</i>' in text


# ---------- T-017：_display_maps 维度映射与页面推荐语展示 ----------


def test_display_maps_dimension_mapping(tmp_path, monkeypatch):
    """_display_maps 按 (dimension, period_label) 取推荐语：周/季/总星各取各的，互不串维度。"""
    from app.web.routes import _display_maps

    db = tmp_path / "map.db"
    init_db(db)
    conn = get_conn(db)
    try:
        now = datetime.now(timezone.utc)
        iso = now.date().isocalendar()
        week_label = f"{iso.year}-W{iso.week:02d}"
        quarter_label = f"{now.year}-Q{(now.month - 1) // 3 + 1}"
        repo_id = _add_repo(
            conn,
            "a/one",
            language="Python",
            description_en="desc",
            snapshots=[(f"{SNAP_DAY}T00:00:00Z", 100)],
        )
        conn.execute(
            "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
            " VALUES (?, 'week', ?, '周文本', NULL, ?), (?, 'quarter', ?, '季文本', NULL, ?),"
            " (?, 'total', 'all', '总星文本', NULL, ?)",
            (repo_id, week_label, week_label, repo_id, quarter_label, week_label, repo_id, week_label),
        )
        conn.commit()
        reasons_w, _, _, _ = _display_maps(conn, dimension="week", period_label=week_label)
        assert reasons_w == {"a/one": "周文本"}
        reasons_q, _, _, _ = _display_maps(conn, dimension="quarter", period_label=quarter_label)
        assert reasons_q == {"a/one": "季文本"}
        reasons_t, _, _, _ = _display_maps(conn, dimension="total", period_label="all")
        assert reasons_t == {"a/one": "总星文本"}
        # 缺该维度行 → 无推荐语块（dict 空，降级形态）
        reasons_empty, _, _, _ = _display_maps(conn, dimension="week", period_label="2026-W01")
        assert reasons_empty == {}
    finally:
        conn.close()


def test_display_maps_summary_fixed_mapping(tmp_path, monkeypatch):
    """T-024：概要展示映射固定取 dimension='summary' 且 period_label='all'（无维度概念，全页面同一条）；
    缺该维度行 → 无概要（dict 空，降级形态）。"""
    from app.web.routes import _display_maps

    db = tmp_path / "map.db"
    init_db(db)
    conn = get_conn(db)
    try:
        repo_id = _add_repo(
            conn,
            "a/one",
            language="Python",
            description_en="desc",
            snapshots=[(f"{SNAP_DAY}T00:00:00Z", 100)],
        )
        conn.execute(
            "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
            " VALUES (?, 'summary', 'all', '概要文本', NULL, '2026-W32'),"
            " (?, 'summary', '2026-W31', '概要文本-旧期', NULL, '2026-W31')",
            (repo_id, repo_id),
        )
        conn.commit()
        _, _, _, summaries = _display_maps(conn, dimension="total", period_label="all")
        assert summaries == {"a/one": "概要文本"}  # 只取 ('summary', 'all')，旧期标签行不展示
        conn.execute("DELETE FROM recommendations WHERE dimension = 'summary' AND period_label = 'all'")
        conn.commit()
        _, _, _, summaries_gone = _display_maps(conn, dimension="total", period_label="all")
        assert summaries_gone == {}  # 缺概要行 → dict 空（降级形态，不留空框）
    finally:
        conn.close()


def test_total_page_shows_total_dimension_reason(client):
    """总星榜页展示 ('total','all') 维度文本（§8.1）；行内推荐按钮按 total 维度渲染。

    T-026 适配：走 /total?board=all（a/py/a/go 不在默认首榜）。"""
    text = client.get("/total?board=all").text
    assert "本周亮点：测试推荐语" in text  # total 行文本（_seed_full 插入）
    assert "<b>总星榜推荐语</b>" in text  # 块标题维度标识（2026-08-12 复验反馈钉死）
    assert 'class="recommend-btn" data-repo="a/py" data-dim="total" data-period-label="all" data-has-reason="1">重新生成</button>' in text
    assert 'data-has-reason="0">生成推荐语</button>' in text  # a/go 无推荐语 → "生成推荐语"


def test_week_page_shows_week_dimension_reason_with_label(client):
    """历史周页展示 ('week', 2026-W32) 维度文本＋维度标题"周榜推荐语 · 期次"（§8.1＋2026-08-12 复验反馈）。

    历史周页 as_of 固定（2026-08-09 期末，起点 08-02 span=7 恒出席），时间稳健——当期 / 断言会随
    "今天"滑窗转红，故不走默认周页。
    T-026 适配：board=all 全量语境（默认单榜 language-java 空榜无行）。
    """
    text = client.get("/?week=2026-W32&board=all").text
    assert "周报亮点：周推荐语" in text  # week 行文本（_seed_full 按固定期次标签插入）
    assert "<b>周榜推荐语 · 2026-W32</b>" in text  # 维度标题带期次：与总星榜文本一眼可辨


def test_quarter_page_shows_quarter_dimension_reason_with_label(client):
    """季页展示 ('quarter', 2026-Q3) 维度文本＋维度标题"季榜推荐语 · 期次"（§8.1＋评审 F3-2 补锁）。

    种子 05-11→08-09 跨度 90 天落在 86~94 季窗口内：历史季页 as_of 恒 2026-09-30（期末端点 08-09、
    起点候选 05-11 恒出席）；当前季取 now 时 08-09 仍为期末端点，两种取值下均出席，时间稳健。
    T-026 适配：board=all 全量语境（默认单榜 language-java 空榜无行）。
    """
    text = client.get("/quarter?quarter=2026-Q3&board=all").text
    assert "季报亮点：季推荐语" in text  # quarter 行文本（_seed_full 按固定期次标签插入）
    assert "<b>季榜推荐语 · 2026-Q3</b>" in text  # 维度标题带期次：与总星榜文本一眼可辨


def test_follows_page_has_recommend_button(follows_client):
    """关注页 P6 行内推荐按钮就位（§8.2：total 维度操作）；无推荐语时按钮按态"生成推荐语"。"""
    text = follows_client.get("/follows").text
    assert 'class="recommend-btn" data-repo="f/ts-hot" data-dim="total" data-period-label="all" data-has-reason="0">生成推荐语</button>' in text


# ---------- T-024：AI 概要块展示（§11：推荐语块下方、全页面同一条；缺则无块） ----------


def test_row_summary_block_on_history_week_page(client):
    """历史周页（as_of 固定，时间稳健）：行面板推荐语块下方渲染"AI 概要"块＋概要文本；
    无概要行的仓（a/rise 新区行）不渲染概要块。

    T-026 适配：board=all 全量语境（默认单榜 language-java 空榜无行）。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=all").text
    assert "概要文本：Python 库" in text and "概要文本：Go 库" in text  # 概要文本随行渲染
    # a/py、a/go 各跨两个榜（语言榜＋主题榜）渲染两行 → 4 块；a/rise 新区行无概要行 → 无块（降级）
    assert text.count('<div class="summary"><b>AI 概要</b>') == 4
    assert text.index("周报亮点：周推荐语") < text.index("概要文本：Python 库")  # 概要块位于推荐语块下方（模板顺序 reason → summary）


def test_row_summary_block_on_follows_page(follows_client):
    """关注页 P6：关注的仓渲染概要块（a/go 有 summary 行，seed 自造）；无概要的仓无块。"""
    text = follows_client.get("/follows").text
    assert "概要文本：Go 库" not in text  # follows_client 是 _seed_follows（无概要行）→ 全页无概要块（降级）
    assert '<div class="summary">' not in text


def test_row_summary_block_on_follows_page_seeded(client):
    """关注页 P6（_seed_full 语境走关注页）：a/go 被关注且有 summary 行 → 概要块渲染（全页面同一条）。"""
    resp = client.get("/follows")
    assert resp.status_code == 200
    text = resp.text
    assert "概要文本：Go 库" in text
    assert text.count('<div class="summary"><b>AI 概要</b>') == 1  # 仅 a/go（关注集只有它）


def test_no_summary_row_renders_no_summary_block(fresh_client):
    """降级：库内无任何 summary 行 → 页面无 .summary 元素（不留空框，AI 失败永不阻塞出榜）。"""
    text = fresh_client.get("/total").text
    assert "AI 概要" not in text and 'class="summary"' not in text


# ---------- T-018：新崛起区（决策 4 v2；内容断言走历史期次页/total，遵守本文件时间稳健约定） ----------


def test_week_history_page_rising_section(client):
    """新区小节渲染（历史周页 as_of 固定，时间稳健）：区头/小字说明/计数徽标/入池标注 +X（入池 N 天）。

    T-026 适配：board=all 全量语境（a/rise 属 Rust＋cli，不在默认首榜 language-java）。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=all").text
    assert "新崛起 · 入池未满一个统计窗口" in text
    assert "以下项目入池不足 7 天，按入池以来增星排序" in text
    assert "a/rise" in text
    assert "+300（入池 3 天）" in text  # 在池增量 + 在池天数整数化标注（3.0 天 → 3）
    assert "新崛起 · 入池未满一个统计窗口<span class=\"n\">Top 1</span>" in text  # 计数徽标 = 新区实际行数
    assert 'class="window-note"' not in text  # 新区行不显示 window_note


def test_quarter_history_page_rising_section(client):
    """季页新区同款（历史季页 as_of 固定）：小字说明 90 天版＋入池标注。

    T-026 适配：board=all 全量语境（a/rise 不在默认首榜）。"""
    text = client.get("/quarter?quarter=2026-Q3&board=all").text
    assert "新崛起 · 入池未满一个统计窗口" in text
    assert "以下项目入池不足 90 天，按入池以来增星排序" in text
    assert "+300（入池 3 天）" in text


def test_rising_section_absent_on_total_and_empty_boards(client):
    """空区不渲染：total 页无新区小节（区头/说明全不出现）；无缺席仓的榜块不渲染区头。"""
    text = client.get("/total").text
    assert "新崛起" not in text
    assert "入池未满" not in text
    # T-026 适配：board=all 全量语境（默认单榜只渲染 language-java 空榜）
    text = client.get(f"/?week={WEEK_LABEL}&board=all").text
    # _seed_full 中仅 a/rise（Rust＋cli）缺席：语言 rust 榜与主题 devtools 榜各一个新区小节，其余榜无区头
    assert text.count("新崛起 · 入池未满一个统计窗口") == 2


def test_sidebar_badges_exclude_rising_rows(client):
    """T-021：边栏项徽标只计主榜行数不计新区（历史周页 as_of 固定）：rust 榜主榜 0 行但新区 1 行，徽标仍 0。

    T-026 适配：边栏项改整页链接（href 携带 board= 与当前 week=，§13.1）；board=all 全量语境。
    （Jinja autoescape：href 中 & 渲染为 &amp;，浏览器点击时还原为 &——断言匹配转义后形态）"""
    text = client.get(f"/?week={WEEK_LABEL}&board=all").text
    assert (
        'class="sb-item" href="/?board=language-rust&amp;week=2026-W32" title="Rust"><span class="sb-dot" style="background:#dea584">'
        '</span><span class="sb-txt">Rust</span><i>0</i></a>' in text
    )
    assert (
        'class="sb-item" href="/?board=language-python&amp;week=2026-W32" title="Python"><span class="sb-dot" style="background:#4b8bbe">'
        '</span><span class="sb-txt">Python</span><i>1</i></a>' in text
    )


def test_fallback_when_main_and_rising_both_empty(tmp_path, monkeypatch):
    """降级判定变更（T-018）：主榜＋新区全空才降级——无任何快照的库周页照旧降级 total＋notice。

    F2-1 收窄适配：straddle 缺席老仓（在池 30 天 > win_max 9）不算新区——主榜＋新区仍全空，降级 notice
    照旧；notice 文案按是否有总星数据分流（"暂无可展示数据"vs"增量榜全部缺席"），两分支都锁。
    """

    def _get_page(db_name, seed):
        db = tmp_path / db_name
        init_db(db)
        conn = get_conn(db)
        seed(conn)
        conn.commit()
        conn.close()
        monkeypatch.setenv("RADAR_DB_PATH", str(db))
        monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
        with TestClient(app) as c:
            return c.get("/")

    # 无任何快照：总星榜也无数据 → "暂无可展示数据"分支（原断言不动）
    resp = _get_page("web-empty.db", lambda conn: _add_repo(conn, "a/no-snap", language="Python", snapshots=[]))
    assert resp.status_code == 200
    assert 'class="notice"' in resp.text
    assert "暂无可展示数据" in resp.text
    assert "新崛起" not in resp.text

    # F2-1 收窄适配：仅 straddle 缺席老仓——主榜缺席且不算新区，降级照旧（有总星数据 → "增量榜全部缺席"分支）
    resp = _get_page(
        "web-straddle.db",
        lambda conn: _add_repo(
            conn,
            "a/straddle",
            language="Go",
            snapshots=[("2026-07-10T00:00:00Z", 50), (f"{SNAP_DAY}T00:00:00Z", 500)],
        ),
    )
    assert resp.status_code == 200
    assert 'class="notice"' in resp.text
    assert "增量榜全部缺席" in resp.text
    assert "新崛起" not in resp.text  # straddle 老仓不算新区（F2-1 收窄）：主榜＋新区全空才降级


# ---------- T-020 端点快照日期标注（流程说明 §10：行详情面板元信息处，全页面共用行模板同步生效） ----------


def test_endpoint_note_slice_shape():
    """_endpoint_note 切片形态：定长 ISO 前 10 字符直接切片（schema 硬约定，不做日期解析）；None → None（模板不渲染）。"""
    from app.web.routes import _endpoint_note

    assert _endpoint_note("2026-08-09T00:00:00Z") == "端点快照 2026-08-09"
    assert _endpoint_note("2026-08-09T05:51:43Z") == "端点快照 2026-08-09"  # 秒级时间戳同日期
    assert _endpoint_note(None) is None


def test_row_endpoint_note_on_history_week_page(client):
    """历史周页（as_of 固定，时间稳健）：主榜行＋新区行面板都渲染端点快照日期标注（_seed_full 端点恒 2026-08-09）。

    T-026 适配：board=all 全量语境（默认单榜 language-java 空榜无行）。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=all").text
    assert text.count("端点快照 2026-08-09") >= 3  # 主榜 a/py、a/go 两行＋新区 a/rise 行


def test_row_endpoint_note_on_total_page(client):
    """total 页端点日期标注（时间稳健：种子端点恒 2026-08-09）。

    T-026 适配：/total?board=all 全量语境（a/py 不在默认首榜）。"""
    assert "端点快照 2026-08-09" in client.get("/total?board=all").text


def test_row_endpoint_note_on_follows_page(follows_client):
    """P6 关注页端点日期标注（快照相对 now 造、日期不固定 → 断言 20XX- 形态）；dead 仓旁路补取同样渲染。"""
    text = follows_client.get("/follows").text
    assert len(re.findall(r"端点快照 20\d\d-\d\d-\d\d", text)) >= 5  # 4 alive 行（含缺席）＋1 dead 行


def test_row_endpoint_note_on_tag_page(client):
    """P5 标签结果页端点日期标注（_seed_full 打标行端点恒 2026-08-09）。"""
    resp = client.get("/tags/选型观察")
    assert resp.status_code == 200
    assert "端点快照 2026-08-09" in resp.text


# ---------- T-022：打标输入 datalist 建议（既有标签全量注入，每页一个） ----------


def test_datalist_present_with_existing_tags(client):
    """渲染行页面（周/季/总星/关注/标签结果页）各注入一个 datalist，含既有标签 option（每页一个防每行一个）。"""
    for path in ("/", f"/?week={WEEK_LABEL}", "/quarter", "/total", "/follows", "/tags/选型观察"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.text.count('<datalist id="all-tags">') == 1, path  # 每页恰好一个
        assert '<option value="选型观察">' in resp.text, path  # 既有标签全量注入
    # 标签云总页无行列表（无打标输入框）：不注入 datalist（设计钉死，防多余渲染）
    assert '<datalist id="all-tags">' not in client.get("/tags").text


def test_datalist_updated_after_adding_tag(client):
    """新增标签后重载页面，datalist 含新标签（服务端全量注入，非页面缓存）。"""
    resp = client.post("/api/tags", json={"full_name": "a/py", "tag": "新标签建议"})
    assert resp.status_code == 200 and resp.json()["added"] is True
    text = client.get("/total").text
    assert text.count('<datalist id="all-tags">') == 1
    assert '<option value="新标签建议">' in text
    assert '<option value="选型观察">' in text  # 既有标签仍在


def test_no_datalist_on_empty_tag_db(fresh_client):
    """空库（无任何标签）不渲染 datalist（钉死形态：无 datalist；JS 对缺失 datalist 的 list 属性静默忽略）。"""
    for path in ("/", "/total", "/follows", "/tags"):
        resp = fresh_client.get(path)
        assert resp.status_code == 200, path
        assert '<datalist id="all-tags">' not in resp.text, path


# ---------- T-021：左侧边栏（§12.1，仅 P1/P2/P3）＋P6 标签筛选（§12.2） ----------


def test_sidebar_on_week_and_quarter_pages(client):
    """P1/P2/P3 榜单页：左侧边栏就位（标题/收起钮/语言+主题两组 17 项/榜内数量徽标）；
    顶部 chips 区与榜头"回顶部"移除（§12.1 拍板：边栏 sticky 常驻后回顶部冗余）。"""
    for path in ("/", f"/?week={WEEK_LABEL}", "/quarter"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        text = resp.text
        assert '<aside class="sidebar"' in text
        assert '<span class="sb-title">榜单直达</span>' in text
        assert 'class="sb-toggle"' in text
        assert 'class="sb-grp">语言' in text and 'class="sb-grp">主题' in text
        assert _sb_item_count(text) == 17  # 语言 7 + 主题 10，顺序 = 榜块顺序
        assert '<div class="chips"' not in text  # 顶部 chips 区已由边栏替代
        assert '<a class="chip" href="#b-' not in text
        assert '<a class="top" href="#top">' not in text  # 榜头回顶部移除
        assert "回顶部" not in text


def test_total_page_keeps_chips_and_top_links(client):
    """P4 总星榜（§12.1 v2.1 修订，T-026 落地）：纳入边栏——边栏就位（17 榜项＋1 全部项）、
    顶部 chips 区移除、每榜头"回顶部"移除（与 P1/P2/P3 一致）。"""
    text = client.get("/total").text
    assert '<aside class="sidebar"' in text
    assert _sb_item_count(text) == 17
    assert _sb_all_count(text) == 1
    assert text.count('class="board"') == 1  # 默认单榜（language-java 空榜）
    assert 'id="b-language-java"' in text
    assert '<div class="chips"' not in text  # 顶部 chips 区已由边栏替代
    assert text.count('<a class="chip" href="#b-') == 0
    assert '<a class="top" href="#top">' not in text  # 榜头回顶部移除
    assert "回顶部" not in text
    # 边栏 href 为 /total?board=xxx（P4 无期次参数）
    assert 'href="/total?board=language-java"' in text
    assert 'href="/total?board=all"' in text


def test_follows_filter_row_and_data_tags(follows_client):
    """P6 筛选行（§12.2）：'全部'带关注总数（默认选中）；各标签 chip 带关注仓计数（0 也列出，字典序）；
    行根带 data-tags 供客户端筛选；空态元素初始隐藏。"""
    text = follows_client.get("/follows").text
    assert '<div class="tag-filter"' in text
    assert '<span class="grp">标签筛选</span>' in text
    assert '<button type="button" class="fchip on" data-tag="">全部<i>5</i></button>' in text
    assert '<button type="button" class="fchip" data-tag="AI">AI<i>0</i></button>' in text  # 未关注仓标签：0 也列出
    assert '<button type="button" class="fchip" data-tag="选型观察">选型观察<i>1</i></button>' in text
    assert text.index('data-tag="AI"') < text.index('data-tag="选型观察"')  # 字典序
    # f/ts-hot 行（客户端筛选数据源；tojson|forceescape JSON 数组编码——实测形态：中文 \uXXXX 转义、引号 &#34;）
    assert f'data-tags="{json.dumps(["选型观察"]).replace(chr(34), "&#34;")}"' in text
    assert text.count('data-tags="[]"') == 4  # 其余四行无标签
    assert 'id="filter-empty" hidden' in text
    assert "该标签下暂无关注项目" in text


def test_follows_filter_row_absent_when_empty(fresh_client):
    """空关注页（§12.2）：不炸（200）；无筛选行与分组区（无行可筛），既有空态引导保留。"""
    resp = fresh_client.get("/follows")
    assert resp.status_code == 200
    text = resp.text
    assert "还没有关注任何项目：去榜单点行右侧 ☆，该项目每周增量会出现在这里" in text
    assert 'class="tag-filter"' not in text
    assert 'id="filter-empty"' not in text
    assert 'id="follow-boards"' not in text


# ---------- T-021 F2-1 回归：data-tags JSON 编码（标签名可含逗号） ----------


def _seed_follows_comma_tags(conn):
    """F2-1 回归种子（独立 fixture，不动 _seed_follows 共享平衡）：两关注仓——f/c-tag 单枚含逗号标签
    `a,b`；f/i-tags 两枚独立标签 `a`/`b`。验证 JSON 数组编码下"逗号标签永不命中/误命中"双向修复。"""
    rows = [
        ("f/c-tag", ["a,b"]),
        ("f/i-tags", ["a", "b"]),
    ]
    for i, (name, tags) in enumerate(rows):
        repo_id = _add_repo(
            conn,
            name,
            language="Python",
            description_en=f"{name} desc",
            snapshots=[(_iso_ago(days=7), 100), (_iso_ago(hours=1), 200)],
        )
        conn.execute("INSERT INTO follows (repo_id, created_at) VALUES (?, ?)", (repo_id, f"2026-08-0{i + 1}T00:00:00Z"))
        for t in tags:
            conn.execute("INSERT INTO tags (repo_id, tag) VALUES (?, ?)", (repo_id, t))


@pytest.fixture()
def follows_comma_client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, _seed_follows_comma_tags) as c:
        yield c


def test_follows_filter_comma_tag_json_encoding(follows_comma_client):
    """F2-1 回归：data-tags JSON 数组编码——含逗号标签 `a,b` 是单枚（chip 计数 1），独立标签 `a`/`b` 各计数 1，
    两行 data-tags 分别精确为 JSON ["a,b"] 与 ["a", "b"]（实测转义形态：&#34;），互不误命中；
    SQL 精确计数与 JSON 编码口径一致即修复成立（客户端命中由浏览器预演覆盖）。"""
    text = follows_comma_client.get("/follows").text
    assert '<button type="button" class="fchip on" data-tag="">全部<i>2</i></button>' in text
    assert '<button type="button" class="fchip" data-tag="a">a<i>1</i></button>' in text
    assert '<button type="button" class="fchip" data-tag="a,b">a,b<i>1</i></button>' in text  # 含逗号标签是单枚 chip
    assert '<button type="button" class="fchip" data-tag="b">b<i>1</i></button>' in text
    assert 'data-tags="[&#34;a,b&#34;]"' in text  # f/c-tag：单枚含逗号标签（JSON ["a,b"]）
    assert 'data-tags="[&#34;a&#34;, &#34;b&#34;]"' in text  # f/i-tags：两枚独立标签（tojson 分隔符含空格）
    assert text.count('data-tags="[&#34;') == 2  # 仅两行带标签 JSON，互不混淆


# ---------- T-026：单榜整页（§13.1）＋P4 纳入边栏（§12.1 v2.1） ----------
# 内容断言全部走历史期次页（as_of 固定时间稳健）＋board 参数叠加，遵守本文件时间稳健约定。


def _sb_item_count(text: str) -> int:
    """边栏榜项数（T-026 起 active 由服务端渲染）：纯 `class="sb-item"` 项 + `class="sb-item active"` 项
    （两形态精确子串互斥：active 项不落入纯形态计数），任一 active 组合下总数恒 = 17 榜项。"""
    return text.count('class="sb-item"') + text.count('class="sb-item active"')


def _sb_all_count(text: str) -> int:
    """边栏"全部"项数（同 _sb_item_count 口径）：非 active 与 active 两形态互斥，合计恒 = 1。"""
    return text.count('class="sb-item sb-all"') + text.count('class="sb-item sb-all active"')


def test_board_param_renders_only_that_board(client):
    """指定 board 只渲染该榜内容（行＋新区＋空态），其余 15 榜只供边栏徽标（17 项仍在）；
    单榜主题榜同样只渲染当前主题榜块（另一组 section 不渲染）。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=language-python").text
    assert text.count('class="board"') == 1
    assert 'id="b-language-python"' in text
    assert text.count('id="b-language-') == 1 and text.count('id="b-topic-') == 0
    assert "a/py" in text  # 历史周出席行
    assert "a/go" not in text  # Go 仓不在 Python 榜：不渲染
    assert "新崛起" not in text  # python 榜无新区（a/rise 属 Rust＋cli）
    assert _sb_item_count(text) == 17  # 其余 15 榜徽标照常（只计数）
    assert _sb_all_count(text) == 1
    # 单榜主题榜：只渲染 ai 主题榜块
    text_ai = client.get(f"/?week={WEEK_LABEL}&board=topic-ai").text
    assert text_ai.count('class="board"') == 1
    assert 'id="b-topic-ai"' in text_ai
    assert text_ai.count('id="b-language-') == 0 and text_ai.count('id="b-topic-') == 1
    assert "a/py" in text_ai  # a/py 命中 ai 主题（pytorch）
    assert "a/go" not in text_ai


def test_single_board_rising_section(client):
    """单榜模式当前榜内容区 = 主榜行＋新崛起区＋空态文案（§13.1）：历史周 rust 榜新区 a/rise 照常渲染。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=language-rust").text
    assert text.count('class="board"') == 1 and 'id="b-language-rust"' in text
    assert "新崛起 · 入池未满一个统计窗口" in text
    assert "a/rise" in text
    assert "+300（入池 3 天）" in text


def test_invalid_board_falls_back_to_first_board(client):
    """非法 board → 静默降级默认首榜（不报错页，§13.1）。"""
    for bad in ("foo", "b-language-java", ""):
        resp = client.get(f"/?board={bad}")
        assert resp.status_code == 200, bad
        assert resp.text.count('class="board"') == 1, bad
        assert 'id="b-language-java"' in resp.text, bad


def test_history_week_board_param_and_sidebar_href(client):
    """P1 历史周页 week+board 叠加生效；边栏整页链接携带当前 week=（§13.1）；"全部"项也在；
    最新周（无 week 参数）边栏 href 不带期次。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=language-python").text
    assert text.count('class="board"') == 1 and 'id="b-language-python"' in text
    # href 中 & 经 Jinja autoescape 渲染为 &amp;（浏览器点击时还原为 &，跳转语义不变）
    assert 'class="sb-item active" href="/?board=language-python&amp;week=2026-W32"' in text
    assert 'href="/?board=language-java&amp;week=2026-W32"' in text
    assert 'class="sb-item sb-all" href="/?board=all&amp;week=2026-W32"' in text
    text_now = client.get("/?board=language-python").text
    assert 'href="/?board=language-java"' in text_now
    assert 'class="sb-item sb-all" href="/?board=all"' in text_now


def test_quarter_board_param_carries_quarter(client):
    """P3 季度页 board 叠加：?quarter=2026-Q3&board=language-python 渲染 python 榜（a/py 历史季恒出席）；
    边栏 href 携带当前 quarter=（§13.1）。"""
    text = client.get("/quarter?quarter=2026-Q3&board=language-python").text
    assert text.count('class="board"') == 1 and 'id="b-language-python"' in text
    assert "a/py" in text  # 05-11→08-09 跨度 90 天恒出席（历史季 as_of 固定）
    # href 中 & 经 Jinja autoescape 渲染为 &amp;（浏览器点击时还原为 &，跳转语义不变）
    assert 'class="sb-item active" href="/quarter?board=language-python&amp;quarter=2026-Q3"' in text
    assert 'href="/quarter?board=language-java&amp;quarter=2026-Q3"' in text
    assert 'class="sb-item sb-all" href="/quarter?board=all&amp;quarter=2026-Q3"' in text


def test_sidebar_active_rendered_server_side(client):
    """当前榜项 active 由服务端渲染（原 scrollspy 移除，§13.1）：单榜页对应项高亮、其余项不高亮；
    默认首榜与 all 全量各自高亮对应项。"""
    text = client.get("/?board=topic-ai").text
    assert 'class="sb-item active" href="/?board=topic-ai"' in text
    assert 'class="sb-item active" href="/?board=language-java"' not in text  # 非当前榜不高亮
    assert 'class="sb-item sb-all active"' not in text
    text_default = client.get("/").text
    assert 'class="sb-item active" href="/?board=language-java"' in text_default  # 默认首榜高亮
    assert 'class="sb-item sb-all active"' not in text_default
    text_all = client.get("/?board=all").text
    assert 'class="sb-item sb-all active"' in text_all  # 全量：全部项高亮


def test_single_board_sidebar_badges_match_full(client):
    """单榜模式边栏徽标与全量模式一致（其余 15 榜只 count，不因单榜化失真）：
    _seed_full 历史周——Java 0 行、Python 1 行、ai 主题 1 行、后端/云原生 0 行、other 主题 1 行（a/go）。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=language-python").text
    assert (
        'title="Java"><span class="sb-dot" style="background:#c9842a"></span><span class="sb-txt">Java</span><i>0</i></a>'
        in text
    )
    assert (
        'title="Python"><span class="sb-dot" style="background:#4b8bbe"></span><span class="sb-txt">Python</span><i>1</i></a>'
        in text
    )
    assert 'title="AI与智能"><span class="sb-dot" style="background:#8b98a9"></span><span class="sb-txt">AI与智能</span><i>1</i></a>' in text
    assert 'title="后端/云原生"><span class="sb-dot" style="background:#8b98a9"></span><span class="sb-txt">后端/云原生</span><i>0</i></a>' in text
    assert 'title="其他"><span class="sb-dot" style="background:#8b98a9"></span><span class="sb-txt">其他</span><i>1</i></a>' in text


def test_fallback_page_ignores_board_param(tmp_path, monkeypatch):
    """首期降级页维持全量现状（§13.1：不单榜化）：带 board 参数仍全量 17 榜＋notice＋当前边栏项="全部"。"""

    def _get_page(db_name):
        db = tmp_path / db_name
        init_db(db)
        conn = get_conn(db)
        _add_repo(conn, "a/no-snap", language="Python", snapshots=[])
        conn.commit()
        conn.close()
        monkeypatch.setenv("RADAR_DB_PATH", str(db))
        monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
        with TestClient(app) as c:
            return c.get("/?board=language-java")

    resp = _get_page("web-fallback-board.db")
    assert resp.status_code == 200
    assert 'class="notice"' in resp.text and "暂无可展示数据" in resp.text
    assert resp.text.count('class="board"') == 17  # 降级页全量（board 参数忽略，不单榜化）
    assert _sb_item_count(resp.text) == 17
    assert 'class="sb-item sb-all active"' in resp.text  # 当前边栏项="全部"（降级页非单榜）


def _seed_partial_boards_old_repos(conn):
    """F2-1 回归种子（独立种子函数，不动 _seed_full/_seed_first_day 共享 fixture 平衡）：
    历史周部分榜有行＋全库无新区行——a/py 两端快照跨度 7 天（≥ 周窗口下限 5 天）出席进 python 榜主榜；
    不造任何缺席仓（单快照仓会进新区，见 _seed_first_day）→ 全库无新区行，锁定"主榜部分有行"是唯一非空来源。

    复现场景：?board=language-java（当前榜空）时非当前榜 python 有出席行——修复前判定式 all(not b.rows ...)
    只认 rows，单榜模式非当前榜 rows 恒空 → 误判全空触发降级；修复后 count 参与判定（count>0 ⟺ 归桶非空
    ⟺ 全量模式该榜 rows 非空），两模式判定恒等，不再误降级。"""
    _add_repo(
        conn,
        "a/py",
        language="Python",
        topics=[],
        description_en="Python lib",
        snapshots=[("2026-08-02T00:00:00Z", 300), (f"{SNAP_DAY}T00:00:00Z", 400)],
    )


def test_single_board_fallback_equivalence_partial_boards(tmp_path, monkeypatch):
    """F2-1（k3 初审中）：单榜模式降级判定与全量模式恒等——部分榜有行时，主榜为空的单榜 URL
    不再误触发降级。

    种子＝历史周（WEEK_LABEL，as_of 恒 2026-08-09 期末）部分榜有行＋全库无新区行；?board=language-java
    （当前榜空）→ 200、无降级 notice（class="notice" 不出现）、当前榜块渲染既有空态"本期暂无数据"；
    对照 ?board=all 同期次正常渲染全量（a/py 出席行在，本就不降级）。"""
    with _make_client(tmp_path, monkeypatch, _seed_partial_boards_old_repos) as c:
        resp = c.get(f"/?week={WEEK_LABEL}&board=language-java")
        assert resp.status_code == 200
        assert 'class="notice"' not in resp.text  # 修复前此处误触发降级（假 notice 文案）
        assert resp.text.count('class="board"') == 1  # 未降级：仍单榜页
        assert 'id="b-language-java"' in resp.text
        assert "本期暂无数据" in resp.text  # 当前榜空态照常
        assert 'class="sb-item sb-all active"' not in resp.text  # 未降级：当前边栏项不是"全部"
        text_all = c.get(f"/?week={WEEK_LABEL}&board=all").text
        assert 'class="notice"' not in text_all  # 对照：同期全量模式本就不降级
        assert "a/py" in text_all
        assert text_all.count('class="board"') == 17
