"""T-038 榜单"隐藏已打标"开关测试（§18）：hide_tagged=1 服务端过滤已打标行（展示装配层，不动榜单口径）。

TestClient 打真实 app（lifespan 跑 init_db），tmp_path 独立库（RADAR_DB_PATH 覆盖）＋调度关闭，
不 mock 内部 DB（与 tests/test_web.py 同口径）。内容断言全走历史期次页（/?week=2026-W32，
as_of 固定、种子快照恒出席，时间稳健）与 /total（最新快照降序，无窗口概念）。

口径锚点：
- 仅 hide_tagged=1 为开，缺省/空值/其他取值静默按关；
- 过滤范围＝主榜/新崛起区/新项目区三区，"已打标"＝tags 表存在任意标签（不看内容/分类）；
- 隐藏计数 N 按行计（同仓多榜重复出现按行分别计；一仓多标签一行只计一次）；N=0 不出计数提示；
- 榜头/区头/边栏徽标保持过滤前口径；首期降级判定跑在过滤前；
- 开态下边栏/期次控件/开关互跳链接携带 hide_tagged=1，关态一律不带。
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn, init_db
from app.main import app

SNAP_DAY = "2026-08-09"  # 2026-W32 周日（同 tests/test_web.py 口径：历史周 as_of 固定，时间稳健）
WEEK_LABEL = "2026-W32"


def _add_repo(conn, name, *, language=None, snapshots=(), github_created_at=None):
    """插一个仓库及其快照，返回 repo_id；created_at 取最早快照日（与真实采集一致）。"""
    created_at = min((ts for ts, _ in snapshots), default=SNAP_DAY)
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at, github_created_at)"
        " VALUES (?, ?, ?, NULL, ?, '[]', 0, 'test', ?, ?)",
        (name, f"node-{name}", f"{name} desc", language, created_at, github_created_at),
    )
    repo_id = cur.lastrowid
    for captured_at, stars in snapshots:
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, captured_at, stars),
        )
    return repo_id


def _seed_zones(conn):
    """三区各有已打标/未打标行（历史周 2026-W32 窗口两端快照出席）：

    - 主榜（language-python）：a/tagged-main（打标，两枚标签——验证按行计不按标签计）、a/plain-main；
    - 新崛起区（language-rust）：a/tagged-rise（打标，在池 3 天 <7）、a/plain-rise；
    - 无 topics → 全部落 topic-other 榜：board=all 时每仓在语言榜＋主题榜各出现一次（按行分别计）。
    """
    tagged_main = _add_repo(
        conn, "a/tagged-main", language="Python",
        snapshots=[("2026-08-02T00:00:00Z", 100), (f"{SNAP_DAY}T00:00:00Z", 500)],
    )
    _add_repo(
        conn, "a/plain-main", language="Python",
        snapshots=[("2026-08-02T00:00:00Z", 100), (f"{SNAP_DAY}T00:00:00Z", 300)],
    )
    tagged_rise = _add_repo(
        conn, "a/tagged-rise", language="Rust",
        snapshots=[("2026-08-06T00:00:00Z", 100), (f"{SNAP_DAY}T00:00:00Z", 400)],
    )
    _add_repo(
        conn, "a/plain-rise", language="Rust",
        snapshots=[("2026-08-06T00:00:00Z", 100), (f"{SNAP_DAY}T00:00:00Z", 200)],
    )
    conn.execute("INSERT INTO tags (repo_id, tag) VALUES (?, '已读')", (tagged_main,))
    conn.execute("INSERT INTO tags (repo_id, tag) VALUES (?, '回头看')", (tagged_main,))  # 同仓第二枚标签
    conn.execute("INSERT INTO tags (repo_id, tag) VALUES (?, '已读')", (tagged_rise,))


def _seed_all_hidden(conn):
    """language-go 榜唯一行已打标（榜块全隐藏空态用）；a/plain 出席防首期降级干扰。"""
    tagged_go = _add_repo(
        conn, "a/tagged-go", language="Go",
        snapshots=[("2026-08-02T00:00:00Z", 100), (f"{SNAP_DAY}T00:00:00Z", 500)],
    )
    _add_repo(
        conn, "a/plain", language="Python",
        snapshots=[("2026-08-02T00:00:00Z", 100), (f"{SNAP_DAY}T00:00:00Z", 300)],
    )
    conn.execute("INSERT INTO tags (repo_id, tag) VALUES (?, '已读')", (tagged_go,))


def _seed_first_day_tagged(conn):
    """首期降级场景：straddle 缺席老仓（在池 30 天 > win_max 9，主榜缺席且不算新区/新项目区，
    三区全空 → 降级总星榜，同 tests/test_web.py 的 F2-1 种子形态）且该仓已打标。"""
    repo = _add_repo(
        conn, "a/old-tagged", language="Go",
        snapshots=[("2026-07-10T00:00:00Z", 50), (f"{SNAP_DAY}T00:00:00Z", 500)],
    )
    conn.execute("INSERT INTO tags (repo_id, tag) VALUES (?, '已读')", (repo,))


def _seed_fresh(conn):
    """新项目区一行已打标：51 个老 Python 仓占满主榜 Top50，a/fresh-tagged（创建未满 1 年）出席但被挤出主榜。"""
    for i in range(51):
        _add_repo(
            conn,
            f"o/old-{i:02d}",
            language="Python",
            github_created_at="2019-01-01T00:00:00Z",
            snapshots=[("2026-08-02T00:00:00Z", 1000), (f"{SNAP_DAY}T00:00:00Z", 2000 + i)],
        )
    fresh = _add_repo(
        conn,
        "a/fresh-tagged",
        language="Python",
        github_created_at="2026-01-01T00:00:00Z",
        snapshots=[("2026-08-02T00:00:00Z", 100), (f"{SNAP_DAY}T00:00:00Z", 400)],
    )
    conn.execute("INSERT INTO tags (repo_id, tag) VALUES (?, '已读')", (fresh,))


def _make_client(tmp_path, monkeypatch, seed):
    db = tmp_path / "hide.db"
    init_db(db)
    conn = get_conn(db)
    seed(conn)
    conn.commit()
    conn.close()
    # get_settings 每次调 getenv 无缓存，RADAR_DB_PATH 请求级生效；调度关闭（tests/test_web.py 同口径）
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    return TestClient(app)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, _seed_zones) as c:
        yield c


def _hidden_count(text: str) -> int | None:
    """从页面提取"已隐藏 N 个已打标项目"的 N；无提示行返回 None。"""
    m = re.search(r"已隐藏 (\d+) 个已打标项目", text)
    return int(m.group(1)) if m else None


# ---------- 开/关/非法值 ----------


def test_default_off_renders_tagged_rows(client):
    """缺省关闭：已打标行照常渲染（完整榜单是口径基线）；无计数提示；开关显示"隐藏已打标"。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=language-python").text
    assert "a/tagged-main" in text and "a/plain-main" in text
    assert _hidden_count(text) is None
    assert ">隐藏已打标</a>" in text
    assert "显示全部" not in text


def test_hide_tagged_on_filters_main_board(client):
    """开态：主榜已打标行消失、未打标行保留；N=1（同仓两枚标签一行只计一次）；
    榜头/边栏徽标保持过滤前口径（Top 2、徽标 2 不缩水）。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=language-python&hide_tagged=1").text
    assert "a/tagged-main" not in text
    assert "a/plain-main" in text
    assert _hidden_count(text) == 1
    assert "显示全部" in text
    # 榜头/区头/边栏徽标 = 过滤前完整榜单口径
    assert 'Python<span class="n">Top 2</span>' in text  # 榜头
    assert '主榜 · 按本期增星排序<span class="n">Top 2</span>' in text  # 主榜区头
    assert '<span class="sb-txt">Python</span><i>2</i>' in text  # 边栏徽标不缩水


def test_hide_tagged_filters_rising_zone(client):
    """开态：新崛起区已打标行消失（N=1）；区头徽标保持过滤前 Top 2。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=language-rust&hide_tagged=1").text
    assert "a/tagged-rise" not in text
    assert "a/plain-rise" in text
    assert _hidden_count(text) == 1
    assert '新崛起 · 入池未满一个统计窗口<span class="n">Top 2</span>' in text


def test_hide_tagged_all_boards_count_per_row(client):
    """计数按行不按仓：board=all 时两仓各在语言榜＋主题榜（other）出现一次 → N=4。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=all&hide_tagged=1").text
    assert "a/tagged-main" not in text and "a/tagged-rise" not in text
    assert "a/plain-main" in text and "a/plain-rise" in text
    assert _hidden_count(text) == 4


def test_hide_tagged_invalid_values_silent_off(client):
    """非法值静默按关：hide_tagged=0 / yes / 空值均渲染完整榜单，不报错、不出提示。"""
    for raw in ("0", "yes", ""):
        resp = client.get(f"/?week={WEEK_LABEL}&board=language-python&hide_tagged={raw}")
        assert resp.status_code == 200
        assert "a/tagged-main" in resp.text
        assert _hidden_count(resp.text) is None
        assert ">隐藏已打标</a>" in resp.text


def test_hide_tagged_on_total_page(client):
    """P4 总星榜同样生效：已打标行消失（total 无窗口概念，种子三仓最新快照出席）。"""
    text = client.get("/total?board=language-python&hide_tagged=1").text
    assert "a/tagged-main" not in text
    assert "a/plain-main" in text
    assert _hidden_count(text) == 1


# ---------- 空态 / N=0 / 新项目区 / 首期降级 ----------


def test_board_all_hidden_empty_state(tmp_path, monkeypatch):
    """榜块全隐藏空态：三区行全被隐藏 → 榜头保留＋"本榜 N 个项目均已打标隐藏"，区头与空壳不渲染。"""
    with _make_client(tmp_path, monkeypatch, _seed_all_hidden) as c:
        text = c.get(f"/?week={WEEK_LABEL}&board=language-go&hide_tagged=1").text
    assert 'id="b-language-go"' in text  # 榜块本体在
    assert 'Go<span class="n">Top 1</span>' in text  # 榜头保留（过滤前口径）
    assert "本榜 1 个项目均已打标隐藏" in text
    assert "a/tagged-go" not in text
    assert "本期暂无数据" not in text  # 主榜区空壳不渲染（空态小字替代）
    assert _hidden_count(text) == 1


def test_hide_tagged_n_zero_no_count(tmp_path, monkeypatch):
    """N=0 不出计数：开关开启但本页无行被隐藏 → 只显示"显示全部"互跳，不带"已隐藏"计数。"""
    with _make_client(tmp_path, monkeypatch, _seed_all_hidden) as c:
        text = c.get(f"/?week={WEEK_LABEL}&board=language-python&hide_tagged=1").text
    assert "a/plain" in text  # 未打标行照常
    assert _hidden_count(text) is None
    assert "显示全部" in text


def test_fresh_zone_filtered(tmp_path, monkeypatch):
    """新项目区过滤：a/fresh-tagged 被挤出主榜落入新项目区，开态后新区整区消失；主榜 50 行不动。"""
    with _make_client(tmp_path, monkeypatch, _seed_fresh) as c:
        url = f"/?week={WEEK_LABEL}&board=language-python"
        on = c.get(f"{url}&hide_tagged=1").text
        off = c.get(url).text
    assert "a/fresh-tagged" in off
    assert '新项目 · 创建未满 1 年<span class="n">Top 1</span>' in off  # 关态区头＋过滤前计数
    assert "a/fresh-tagged" not in on
    assert "新项目 · 创建未满 1 年" not in on  # 新区空区不渲染（区头不出现）
    assert _hidden_count(on) == 1
    assert "o/old-50" in on  # 主榜 Top50 行不受影响（隐藏不补位）


def test_fallback_decision_unaffected_by_hide(tmp_path, monkeypatch):
    """首期降级判定跑在过滤前：三区全空 → 降级总星榜照常（notice 在）；降级渲染的行同样被过滤。"""
    with _make_client(tmp_path, monkeypatch, _seed_first_day_tagged) as c:
        text = c.get("/?hide_tagged=1").text
    assert 'class="notice"' in text  # 降级照常发生（隐藏不抑制降级）
    assert "增量榜全部缺席" in text
    assert "a/old-tagged" not in text  # 降级总星榜的已打标行同样被过滤
    # 该仓在降级的语言榜＋主题榜各一行 → N=2，两榜各自进入全隐藏空态
    assert _hidden_count(text) == 2
    assert text.count("本榜 1 个项目均已打标隐藏") == 2


# ---------- URL 透传 ----------


def test_url_propagation_when_on(client):
    """开态透传：边栏项（含 week= 组合）、期次分段控件 prev/next、开关互跳链接均携带 hide_tagged=1。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=all&hide_tagged=1").text
    # 边栏项：board= 与 week= 之外追加 hide_tagged=1
    assert 'href="/?board=language-rust&amp;week=2026-W32&amp;hide_tagged=1"' in text
    assert 'href="/?board=all&amp;week=2026-W32&amp;hide_tagged=1"' in text
    # 期次分段控件（种子最早入池 2026-08-02 → W31 有上一期；当前周 > W32 有下一期）
    assert 'href="/?week=2026-W31&amp;hide_tagged=1"' in text
    assert 'href="/?week=2026-W33&amp;hide_tagged=1"' in text
    # 开关互跳（关回完整榜单）：保留 board=/week=，不带 hide_tagged
    assert '<a href="/?board=all&amp;week=2026-W32">显示全部</a>' in text


def test_url_clean_when_off(client):
    """关态链接一律不带 hide_tagged：边栏/期次控件无该参数；开关链接 = 当前 URL 追加 hide_tagged=1。"""
    text = client.get(f"/?week={WEEK_LABEL}&board=language-python").text
    assert 'href="/?board=language-rust&amp;week=2026-W32&amp;hide_tagged=1"' not in text
    assert 'href="/?board=language-rust&amp;week=2026-W32"' in text
    assert 'href="/?week=2026-W31&amp;hide_tagged=1"' not in text
    assert 'href="/?week=2026-W31"' in text
    # 开关互跳（开）：当前页 URL 追加 hide_tagged=1
    assert '<a href="/?board=language-python&amp;week=2026-W32&amp;hide_tagged=1">隐藏已打标</a>' in text


def test_total_page_propagation(client):
    """P4 无期次概念：开态边栏链接携带 hide_tagged=1（无 week=/quarter=）。"""
    text = client.get("/total?board=all&hide_tagged=1").text
    assert 'href="/total?board=language-rust&amp;hide_tagged=1"' in text
    assert 'href="/total?board=all&amp;hide_tagged=1"' in text
    assert '<a href="/total?board=all">显示全部</a>' in text
