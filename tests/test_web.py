"""T-008 榜单页面测试：TestClient 打真实 app（lifespan 会跑 init_db），tmp_path 独立库（RADAR_DB_PATH 覆盖）+ 调度关闭。

断言分两类：
- 结构断言（时间稳健）：三页面 200、17 榜区块、17 chips、默认展开态、空态降级提示、期次参数 200/400；
  周/季出席数据随"今天"推移会滑出窗口，故不做数据内容断言（口径语义归 tests/test_report.py）。
- 内容断言全走 /total（最新快照降序，无窗口概念，时间稳健）与首期空态（快照仅同一天）。
"""

import json
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
    conn.execute("INSERT INTO follows (repo_id, created_at) VALUES (?, ?)", (repo_go, "2026-08-02T00:00:00Z"))
    # T-017 推荐语按 (dimension, period_label) 展示：总星行文本供 /total（时间稳健）断言；
    # 周/季行按"今天"所处期次标签插入（_display_maps 维度映射单测另行覆盖）
    now = datetime.now(timezone.utc)
    iso = now.date().isocalendar()
    week_label = f"{iso.year}-W{iso.week:02d}"
    quarter_label = f"{now.year}-Q{(now.month - 1) // 3 + 1}"
    conn.execute(
        "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
        " VALUES (?, 'total', 'all', '本周亮点：测试推荐语', NULL, ?),"
        " (?, 'week', ?, '周报亮点：周推荐语', NULL, ?),"
        " (?, 'quarter', ?, '季报亮点：季推荐语', NULL, ?)",
        (repo_py, week_label, repo_py, week_label, week_label, repo_py, quarter_label, week_label),
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


def test_index_has_17_boards_and_chips(client):
    """17 榜齐备（语言 7 + 主题 10），空榜也渲染板块；锚点 chips 一一对应。"""
    text = client.get("/").text
    assert text.count('class="board"') == 17  # class="boards" 容器不带右引号，不会被误计
    assert text.count('<a class="chip" href="#b-') == 17


def test_empty_state_falls_back_to_total(fresh_client):
    """首期空态（验收标准）：周榜全缺席 → 显示总星榜内容 + 明确提示；17 板块仍在。"""
    resp = fresh_client.get("/")
    assert resp.status_code == 200
    # 时间稳健口径（评审中-1 修复）：首期库上周榜永远全缺席 → 必显示降级提示条；
    # 具体文案随"今天"与 ready 日的关系变化（倒计时/通用缺席），裸 / 请求不锁字面
    assert 'class="notice"' in resp.text
    assert "a/new" in resp.text  # 总星榜行确实渲染
    assert resp.text.count('class="board"') == 17
    assert "本期暂无数据" in resp.text  # 空榜板块照列
    assert "follows-sec" not in resp.text  # v1.3：P1 顶部关注区已移出为独立页 P6
    # 默认全部展开（D1/D3 v1.1）：行即展开态、面板 open、aria 同步
    assert 'class="row expanded"' in resp.text
    assert 'class="panel open"' in resp.text
    assert 'aria-expanded="true"' in resp.text


def test_history_week_param(fresh_client):
    """历史周次参数：合法周 200 且页内显示该周次；跟踪池建立前的周次显示无数据提示。"""
    resp = fresh_client.get(f"/?week={WEEK_LABEL}")
    assert resp.status_code == 200
    assert WEEK_LABEL in resp.text  # 期次控件当前期标签
    assert "初始总星榜" in resp.text  # 该周（=快照首日所在周）仍是首期空态
    # 倒计时字面锁在固定历史周断言（as_of 恒 2026-08-09 < ready 2026-08-16），时间稳健
    assert "首份周报预计将于" in resp.text
    resp = fresh_client.get(f"/?week={PREV_WEEK_LABEL}")
    assert resp.status_code == 200
    assert "暂无可展示数据" in resp.text  # 2026-08-02 之前无任何快照，连总星榜也为空


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
    """总星榜（时间稳健的内容断言载体）：中文描述两行形态、推荐语块、标签只读 chip、已关注星标实心。"""
    resp = client.get("/total")
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
    """T-009 接线：星标启用（不再 disabled）、带 data-repo 与关注/取消关注 title；toast 容器就位。"""
    text = client.get("/total").text
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
        reasons_w, _, _ = _display_maps(conn, dimension="week", period_label=week_label)
        assert reasons_w == {"a/one": "周文本"}
        reasons_q, _, _ = _display_maps(conn, dimension="quarter", period_label=quarter_label)
        assert reasons_q == {"a/one": "季文本"}
        reasons_t, _, _ = _display_maps(conn, dimension="total", period_label="all")
        assert reasons_t == {"a/one": "总星文本"}
        # 缺该维度行 → 无推荐语块（dict 空，降级形态）
        reasons_empty, _, _ = _display_maps(conn, dimension="week", period_label="2026-W01")
        assert reasons_empty == {}
    finally:
        conn.close()


def test_total_page_shows_total_dimension_reason(client):
    """总星榜页展示 ('total','all') 维度文本（§8.1）；行内推荐按钮按 total 维度渲染。"""
    text = client.get("/total").text
    assert "本周亮点：测试推荐语" in text  # total 行文本（_seed_full 插入）
    assert 'class="recommend-btn" data-repo="a/py" data-dim="total" data-period-label="all" data-has-reason="1">重新生成</button>' in text
    assert 'data-has-reason="0">生成推荐语</button>' in text  # a/go 无推荐语 → "生成推荐语"


def test_follows_page_has_recommend_button(follows_client):
    """关注页 P6 行内推荐按钮就位（§8.2：total 维度操作）；无推荐语时按钮按态"生成推荐语"。"""
    text = follows_client.get("/follows").text
    assert 'class="recommend-btn" data-repo="f/ts-hot" data-dim="total" data-period-label="all" data-has-reason="0">生成推荐语</button>' in text
