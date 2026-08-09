"""T-008 榜单页面测试：TestClient 打真实 app（lifespan 会跑 init_db），tmp_path 独立库（RADAR_DB_PATH 覆盖）+ 调度关闭。

断言分两类：
- 结构断言（时间稳健）：三页面 200、17 榜区块、17 chips、默认展开态、空态降级提示、期次参数 200/400；
  周/季出席数据随"今天"推移会滑出窗口，故不做数据内容断言（口径语义归 tests/test_report.py）。
- 内容断言全走 /total（最新快照降序，无窗口概念，时间稳健）与首期空态（快照仅同一天）。
"""

import json
from datetime import date, timedelta

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
    # 推荐语按 as_of 所在 ISO 周取数：周 key 必须与"今天"同周，否则 /total 面板取不到
    iso = date.today().isocalendar()
    conn.execute(
        "INSERT INTO recommendations (repo_id, report_week, text) VALUES (?, ?, ?)",
        (repo_py, f"{iso.year}-W{iso.week:02d}", "本周亮点：测试推荐语"),
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
    assert "暂无关注" in resp.text  # 关注区空态（follows 为空）
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
    assert "我的关注" not in text  # 关注区仅属周报页（P3/P4 布局同 P1 榜单区，不含关注区）


def test_follow_section_on_weekly(client):
    """关注区置顶（周报页）：follows 有一条 → 渲染卡片与计数。"""
    text = client.get("/").text
    assert "我的关注（1）" in text
    assert "a/go" in text


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
    assert 'id="follows-sec" data-as-of="' in text  # 关注区带页面 as_of：API 渲染新卡沿用同窗口径
