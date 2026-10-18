"""关注（T-009，决策 9 动态入池）测试：假 GitHub client + tmp_path 真实 sqlite 文件，全程离线，禁真打 API。

覆盖任务书钉死口径：关注三态各自的库内状态、幂等重复关注、取消保留跟踪、取消不存在关注、
关注 API 的 400/404/502/409 与幂等；未入池抓取一律 mock（FakeRepoClient）。
不引 pytest-asyncio（不加新依赖）：异步用例统一 asyncio.run 驱动，与 test_github/test_snapshot 同口径。
"""

import asyncio
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.collector.github import GitHubAuthError, GitHubClient, GitHubError, GitHubNotFoundError
from app.db import get_conn, init_db
from app.follows import STATE_ALIVE, STATE_JOINED, STATE_REVIVED, follow_repo, unfollow_repo
from app.main import app
from app.web.routes import _declared_too_large, _github_client

NOW = "2026-08-09T08:00:00Z"  # 关注/基线共享的固定时间戳（UTC 定长硬约定）


def make_repo_item(full_name, *, stars=42, description="demo", language="Go", topics=("cli",), node_id=None, created_at=None):
    """造一条 REST /repos/{full_name} 响应（字段对齐入库子集，与 Search item 同构）。

    created_at（T-033）：GitHub 创建时间，缺省 None（真实响应必有，测试默认不给以锁定"缺失不中断"）。"""
    item = {
        "full_name": full_name,
        "node_id": node_id or f"node-{full_name}",
        "description": description,
        "language": language,
        "topics": list(topics),
        "stargazers_count": stars,
    }
    if created_at is not None:
        item["created_at"] = created_at
    return item


class FakeRepoClient:
    """假单仓库 client：fetch_repo 按名字脚本返回；not_found / error 模拟 404 与上游故障；记录调用供断言。"""

    def __init__(self, items=None, not_found=(), error=None):
        self.items = items or {}
        self.not_found = set(not_found)
        self.error = error
        self.calls: list[str] = []

    async def fetch_repo(self, full_name: str) -> dict:
        self.calls.append(full_name)
        if self.error is not None:
            raise self.error
        if full_name in self.not_found:
            raise GitHubNotFoundError(f"GitHub 资源不存在（HTTP 404）：{full_name}")
        return self.items[full_name]


def _open_db(tmp_path):
    db = tmp_path / "follows.db"
    init_db(db)
    return get_conn(db)


def _add_repo(conn, name, *, dead=0, source="initial", snapshots=()):
    """插一个池内仓库及其快照，返回 repo_id。"""
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, language, topics, dead, source, created_at)"
        " VALUES (?, ?, 'd', 'Go', '[]', ?, ?, '2026-08-01T00:00:00Z')",
        (name, f"node-{name}", dead, source),
    )
    repo_id = cur.lastrowid
    for captured_at, stars in snapshots:
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, captured_at, stars),
        )
    return repo_id


def _follow(conn, client, name):
    return asyncio.run(follow_repo(conn, client, name, now_iso=lambda: NOW))


def _counts(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM star_snapshots").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0],
    )


def _banner_text(html: str) -> str | None:
    """从 P6 页面里取出结果横幅文本（`<p class="notice[...]" role="status">…</p>`）；无横幅返回 None。

    横幅内含可选锚点链接，故取 `</p>` 之前、首个 `<a` 之前的部分——断言的是横幅文案本身。
    """
    match = re.search(r'<p class="notice[^"]*" role="status">(.*?)</p>', html, re.S)
    if match is None:
        return None
    return match.group(1).split("<a ", 1)[0].strip()


def _get_follows(client, location: str) -> tuple[str | None, str]:
    """按 303 的 Location 走一次 GET（PRG 第二步）：返回 (横幅文本, 页面 HTML)。"""
    resp = client.get(location)
    assert resp.status_code == 200
    return _banner_text(resp.text), resp.text


def _assert_follows_skeleton(text: str) -> None:
    """失败页必须是完整 P6 页面：元信息行、关注计数、顶栏计数、添加入口与提示行都在（不是残缺片段）。"""
    assert "与当期周报同口径" in text
    assert 'id="follow-total">' in text
    assert 'id="nav-follow-count">' in text
    assert 'action="/follows/add"' in text
    assert 'id="follow-add-input"' in text
    assert "支持 github.com 链接、git@github.com:owner/repo.git 与 owner/repo；已关注的仓库不会重复写入。" in text


# ---------- 服务层：关注三态与幂等 ----------


def test_follow_alive_only_inserts_follows(tmp_path):
    """态① 已入池 alive：只插 follows 行；不打 GitHub，repos/snapshots 零变化。"""
    conn = _open_db(tmp_path)
    repo_id = _add_repo(conn, "a/alive", snapshots=[("2026-08-02T00:00:00Z", 100), ("2026-08-09T00:00:00Z", 150)])
    client = FakeRepoClient()

    outcome = _follow(conn, client, "a/alive")
    assert outcome.state == STATE_ALIVE and not outcome.already_followed
    assert outcome.repo_id == repo_id
    assert client.calls == []  # 已入池 alive 不耗 GitHub 配额
    follows = conn.execute("SELECT repo_id, created_at FROM follows").fetchall()
    assert [(r["repo_id"], r["created_at"]) for r in follows] == [(repo_id, NOW)]
    row = conn.execute("SELECT dead, source FROM repos WHERE id = ?", (repo_id,)).fetchone()
    assert row["dead"] == 0 and row["source"] == "initial"
    assert conn.execute("SELECT COUNT(*) FROM star_snapshots WHERE repo_id = ?", (repo_id,)).fetchone()[0] == 2
    conn.close()


def test_follow_dead_revives_with_baseline(tmp_path):
    """态② 已入池 dead：复活 dead=0（回每日跟踪选池）＋立即补一张最新基线快照＋follows；元数据不动。"""
    conn = _open_db(tmp_path)
    repo_id = _add_repo(conn, "a/dead", dead=1, source="discover", snapshots=[("2026-08-01T00:00:00Z", 77)])
    client = FakeRepoClient(items={"a/dead": make_repo_item("a/dead", stars=99)})

    outcome = _follow(conn, client, "a/dead")
    assert outcome.state == STATE_REVIVED and not outcome.already_followed
    row = conn.execute("SELECT dead, source FROM repos WHERE id = ?", (repo_id,)).fetchone()
    assert row["dead"] == 0  # 复活：每日 _snapshot_all 选 dead=0 全量，自动回到跟踪
    assert row["source"] == "discover"  # 复活不改元数据（source 保留原入池来源）
    snaps = conn.execute(
        "SELECT captured_at, stars FROM star_snapshots WHERE repo_id = ? ORDER BY captured_at", (repo_id,)
    ).fetchall()
    assert [(s["captured_at"], s["stars"]) for s in snaps] == [("2026-08-01T00:00:00Z", 77), (NOW, 99)]
    assert conn.execute("SELECT 1 FROM follows WHERE repo_id = ?", (repo_id,)).fetchone() is not None
    conn.close()


def test_follow_new_repo_joins_pool(tmp_path):
    """态③ 未入池：repos(source='follow')＋基线快照＋follows 一次落齐，字段映射与采集层同口径。"""
    conn = _open_db(tmp_path)
    client = FakeRepoClient(
        items={"tiny/lib": make_repo_item("tiny/lib", stars=66, description="tiny", language="Rust", topics=("cli", "ai"))}
    )

    outcome = _follow(conn, client, "tiny/lib")
    assert outcome.state == STATE_JOINED and not outcome.already_followed
    assert client.calls == ["tiny/lib"]  # 一次请求拿全
    row = conn.execute("SELECT * FROM repos WHERE full_name = 'tiny/lib'").fetchone()
    assert row["source"] == "follow"
    assert row["node_id"] == "node-tiny/lib"  # 每日 nodes(ids:) 采集入口必须入库
    assert row["description_en"] == "tiny"
    assert row["language"] == "Rust"
    assert row["topics"] == '["cli", "ai"]'  # schema 硬约定：JSON 数组字符串
    assert row["dead"] == 0
    assert row["created_at"] == NOW
    snap = conn.execute("SELECT captured_at, stars FROM star_snapshots WHERE repo_id = ?", (row["id"],)).fetchone()
    assert (snap["captured_at"], snap["stars"]) == (NOW, 66)  # 基线快照：增量两端从这里起算
    assert conn.execute("SELECT created_at FROM follows WHERE repo_id = ?", (row["id"],)).fetchone()[0] == NOW
    conn.close()


def test_follow_via_301_uses_new_name(tmp_path):
    """改名 301 端到端（§21.6）：旧链接 → 跟随跳转 → 按新名入池，outcome 与库内都用新名。

    串起 fetch_repo 的请求级 follow_redirects 与 follow_repo 的规范名回传（两半各自有单测，
    这里是拼接处：301 后的 200 响应 full_name 即新名，入池不再用旧名）。
    """
    conn = _open_db(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(301, headers={"Location": "https://api.github.com/repositories/42"})
        return httpx.Response(200, json=make_repo_item("new/name", stars=5))

    client = GitHubClient("fake-token", transport=httpx.MockTransport(handler))
    try:
        outcome = _follow(conn, client, "old/name")
    finally:
        asyncio.run(client.aclose())
    assert len(calls) == 2  # 旧链接一次 301 + 跟随一次 200
    assert outcome.state == STATE_JOINED and outcome.full_name == "new/name"
    row = conn.execute("SELECT full_name, source FROM repos").fetchone()
    assert (row["full_name"], row["source"]) == ("new/name", "follow")
    assert conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0] == 1
    conn.close()


def test_follow_join_stores_github_created_at(tmp_path):
    """T-033：关注动态入池 → REST created_at 归一化入 repos.github_created_at（带毫秒变体剥毫秒为定长）；
    缺失/畸形 → NULL 且不中断（采集主链路不因展示字段断）。"""
    conn = _open_db(tmp_path)
    client = FakeRepoClient(items={"a/join": make_repo_item("a/join", created_at="2024-05-01T10:00:00.123Z")})
    _follow(conn, client, "a/join")
    row = conn.execute("SELECT github_created_at FROM repos WHERE full_name = 'a/join'").fetchone()
    assert row["github_created_at"] == "2024-05-01T10:00:00Z"  # 毫秒剥除归一化（schema 定长硬约定）
    conn.close()

    conn2 = _open_db(tmp_path / "t2.db")
    client2 = FakeRepoClient(
        items={
            "b/join": make_repo_item("b/join"),  # 缺失
            "c/join": make_repo_item("c/join", created_at="not-a-date"),  # 畸形
        }
    )
    _follow(conn2, client2, "b/join")
    _follow(conn2, client2, "c/join")
    rows2 = {
        r["full_name"]: r["github_created_at"]
        for r in conn2.execute("SELECT full_name, github_created_at FROM repos")
    }
    assert rows2["b/join"] is None and rows2["c/join"] is None  # 缺失/畸形存 NULL 不中断
    conn2.close()


def test_repeat_follow_is_idempotent(tmp_path):
    """幂等重复关注：第二次短路返回 already_followed，零 GitHub 调用、三表零新行（两态各验一遍）。"""
    conn = _open_db(tmp_path)
    # 态③ 重复关注
    client = FakeRepoClient(items={"tiny/lib": make_repo_item("tiny/lib")})
    first = _follow(conn, client, "tiny/lib")
    second = _follow(conn, client, "tiny/lib")
    assert not first.already_followed and second.already_followed
    assert client.calls == ["tiny/lib"]  # 只打过一次 GitHub
    assert _counts(conn) == (1, 1, 1)
    # 态① 重复关注
    repo_id = _add_repo(conn, "a/alive", snapshots=[("2026-08-09T00:00:00Z", 10)])
    client2 = FakeRepoClient()
    _follow(conn, client2, "a/alive")
    again = _follow(conn, client2, "a/alive")
    assert again.already_followed and again.repo_id == repo_id
    assert client2.calls == []  # alive 态从头到尾不打 GitHub
    assert _counts(conn) == (2, 2, 2)
    conn.close()


def test_unfollow_keeps_tracking(tmp_path):
    """取消关注：只删 follows 行；repos/source/dead 与基线快照一律保留（决策 9 跟踪保留）。"""
    conn = _open_db(tmp_path)
    client = FakeRepoClient(items={"tiny/lib": make_repo_item("tiny/lib", stars=66)})
    _follow(conn, client, "tiny/lib")

    assert unfollow_repo(conn, "tiny/lib") is True
    assert conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0] == 0
    row = conn.execute("SELECT dead, source FROM repos WHERE full_name = 'tiny/lib'").fetchone()
    assert row["dead"] == 0 and row["source"] == "follow"  # 仍在每日跟踪池
    assert conn.execute("SELECT COUNT(*) FROM star_snapshots").fetchone()[0] == 1  # 基线保留
    conn.close()


def test_unfollow_not_followed_is_noop(tmp_path):
    """取消不存在的关注幂等成功：在池未关注 / 根本不在池都返回 False、无异常、零变化。"""
    conn = _open_db(tmp_path)
    _add_repo(conn, "a/pooled", snapshots=[("2026-08-09T00:00:00Z", 10)])
    assert unfollow_repo(conn, "a/pooled") is False  # 在池未关注
    assert unfollow_repo(conn, "ghost/none") is False  # 不在池
    assert conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM star_snapshots").fetchone()[0] == 1  # 快照没被动
    conn.close()


def test_follow_missing_repo_raises_not_found(tmp_path):
    """GitHub 404（不存在/已删除/转私有）：NotFound 上抛，三表零变化（关注整体失败不留半写）。"""
    conn = _open_db(tmp_path)
    client = FakeRepoClient(not_found={"ghost/none"})
    with pytest.raises(GitHubNotFoundError):
        _follow(conn, client, "ghost/none")
    assert _counts(conn) == (0, 0, 0)
    conn.close()


def test_follow_node_id_conflict_rolls_back(tmp_path):
    """改名坑（T-006 评审中-1 同型）：node_id 撞库内旧名 → fail loud 且事务回滚，不留半写。"""
    conn = _open_db(tmp_path)
    _add_repo(conn, "old/name")  # node-old/name 在库
    conn.commit()  # 种子先落盘：with conn: 回滚的是整个未决事务，不 commit 会把种子一并卷走（生产路径每请求独立连接无此问题）
    client = FakeRepoClient(items={"new/name": make_repo_item("new/name", node_id="node-old/name")})
    with pytest.raises(ValueError, match="改名"):
        _follow(conn, client, "new/name")
    assert _counts(conn) == (1, 0, 0)  # 只有旧行；无新 repos/快照/follows
    conn.close()


def test_follow_node_id_changed_fails_loud(tmp_path):
    """同名删除重建（T-009 评审中-1）：node_id 已变 → fail loud 且事务回滚，不复活、不留半写。"""
    conn = _open_db(tmp_path)
    _add_repo(conn, "a/rebuilt", dead=1, snapshots=[("2026-08-01T00:00:00Z", 77)])  # node-a/rebuilt 在库
    conn.commit()  # 种子先落盘：与 test_follow_node_id_conflict_rolls_back 同隔离理由
    client = FakeRepoClient(items={"a/rebuilt": make_repo_item("a/rebuilt", stars=99, node_id="node-NEW")})
    with pytest.raises(ValueError, match="node_id 已变化"):
        _follow(conn, client, "a/rebuilt")
    assert _counts(conn) == (1, 1, 0)  # 无新 repos/快照/follows
    assert conn.execute("SELECT dead FROM repos WHERE full_name = 'a/rebuilt'").fetchone()[0] == 1  # 未复活
    conn.close()


# ---------- 服务层：大小写不敏感命中与规范名回传（direct-follow 设计决策 4） ----------


def test_follow_case_insensitive_hits_pooled_record(tmp_path):
    """大小写命中态①：库内 psf/requests，输入 PSF/Requests → 命中同一条、0 次 GitHub 调用、回传库内规范名。"""
    conn = _open_db(tmp_path)
    repo_id = _add_repo(conn, "psf/requests", snapshots=[("2026-08-09T00:00:00Z", 10)])
    client = FakeRepoClient()

    outcome = _follow(conn, client, "PSF/Requests")
    assert outcome.state == STATE_ALIVE and not outcome.already_followed
    assert outcome.repo_id == repo_id  # 命中库内记录，不是新建第二条
    assert outcome.full_name == "psf/requests"  # 规范名 = 库内名字（不是输入的 PSF/Requests）
    assert client.calls == []  # 已入池 alive 不打 GitHub
    assert _counts(conn) == (1, 1, 1)  # 只有一条 repos：没有因大小写差异重复入池
    conn.close()


def test_follow_case_insensitive_idempotent_shortcut(tmp_path):
    """大小写变体的幂等短路：先关注 psf/requests，再以 PSF/Requests 提交 → already_followed、仍 0 次调用。"""
    conn = _open_db(tmp_path)
    repo_id = _add_repo(conn, "psf/requests", snapshots=[("2026-08-09T00:00:00Z", 10)])
    client = FakeRepoClient()

    first = _follow(conn, client, "psf/requests")
    second = _follow(conn, client, "PSF/Requests")
    assert not first.already_followed and second.already_followed
    assert second.repo_id == repo_id and second.full_name == "psf/requests"
    assert client.calls == []  # 幂等短路位置未变：两次都不打 GitHub
    assert _counts(conn) == (1, 1, 1)
    conn.close()


def test_follow_case_insensitive_dead_uses_canonical_name(tmp_path):
    """大小写命中态②：库内 a/dead 为 dead，输入 A/DEAD → 只调用一次且参数是库内规范名 a/dead。

    用规范名打 GitHub 是硬要求：拿输入大小写变体去请求会与库内记录分叉成两条 node_id，撞库误判。
    """
    conn = _open_db(tmp_path)
    _add_repo(conn, "a/dead", dead=1, snapshots=[("2026-08-01T00:00:00Z", 77)])
    client = FakeRepoClient(items={"a/dead": make_repo_item("a/dead", stars=99)})

    outcome = _follow(conn, client, "A/DEAD")
    assert client.calls == ["a/dead"]  # 一次调用，且参数是库内规范名
    assert outcome.state == STATE_REVIVED and outcome.full_name == "a/dead"
    assert conn.execute("SELECT dead FROM repos WHERE full_name = 'a/dead'").fetchone()[0] == 0  # 已复活
    assert _counts(conn) == (1, 2, 1)  # 无新 repo：大小写差异没有造出第二条记录
    conn.close()


def test_follow_unpooled_uses_github_returned_canonical_name(tmp_path):
    """大小写未入池态③：输入 TINY/LIB，GitHub 返回 tiny/lib → 入池与回传都用 GitHub 返回的规范名。"""
    conn = _open_db(tmp_path)
    client = FakeRepoClient(items={"TINY/LIB": make_repo_item("tiny/lib", stars=66, language="Rust")})

    outcome = _follow(conn, client, "TINY/LIB")
    assert client.calls == ["TINY/LIB"]  # 未入池只能用输入值去问 GitHub
    assert outcome.state == STATE_JOINED
    assert outcome.full_name == "tiny/lib"  # 规范名以 GitHub 返回为准
    assert conn.execute("SELECT full_name FROM repos").fetchone()[0] == "tiny/lib"
    conn.close()


# ---------- 关注 API（TestClient + 依赖注入假 client） ----------


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """独立 tmp 库 + 假 GitHub client；RADAR_JOBS_ENABLED=0 关调度（tests/test_web.py 同口径）。"""
    db = tmp_path / "api.db"
    init_db(db)
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    fake = FakeRepoClient(
        items={"tiny/lib": make_repo_item("tiny/lib", stars=66, language="Rust")},
        not_found={"ghost/none"},
    )
    app.dependency_overrides[_github_client] = lambda: fake
    with TestClient(app) as client:
        yield client, fake, db
    app.dependency_overrides.clear()


def test_api_follow_new_repo(api):
    """POST /api/follows 态③：200＋响应形态；库内 repos/follows/基线齐备；v1.3 起响应不带 card_html（P1 无可插入区域）。"""
    client, fake, db = api
    resp = client.post("/api/follows", json={"full_name": "tiny/lib"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["followed"] is True and data["state"] == STATE_JOINED and data["already_followed"] is False
    assert data["follow_count"] == 1
    assert "card_html" not in data  # T-015：关注区移出为独立页 P6，响应不再带局部渲染卡
    conn = get_conn(db)
    try:
        assert conn.execute("SELECT source FROM repos WHERE full_name = 'tiny/lib'").fetchone()[0] == "follow"
        assert conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM star_snapshots").fetchone()[0] == 1
    finally:
        conn.close()
    assert fake.calls == ["tiny/lib"]


def test_api_repeat_follow_idempotent(api):
    """重复 POST 幂等：already_followed=true、计数不变、只打一次 GitHub。"""
    client, fake, _ = api
    client.post("/api/follows", json={"full_name": "tiny/lib"})
    resp = client.post("/api/follows", json={"full_name": "tiny/lib"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["already_followed"] is True and data["follow_count"] == 1
    assert fake.calls == ["tiny/lib"]


def test_api_follow_alive_no_github(api):
    """态①走 API：已入池 alive 只补 follows，零 GitHub 调用。"""
    client, fake, db = api
    conn = get_conn(db)
    conn.execute(
        "INSERT INTO repos (full_name, node_id, language, topics, dead, source, created_at)"
        " VALUES ('a/pooled', 'node-a/pooled', 'Go', '[]', 0, 'initial', '2026-08-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    resp = client.post("/api/follows", json={"full_name": "a/pooled"})
    assert resp.status_code == 200
    assert resp.json()["state"] == STATE_ALIVE
    assert fake.calls == []


def test_api_follow_invalid_input(api):
    """非法输入 400（fail-loud，垃圾不打 GitHub）：非 owner/repo 形态、空串、三段、非对象体、坏 as_of。"""
    client, fake, _ = api
    assert client.post("/api/follows", json={"full_name": "oops"}).status_code == 400
    assert client.post("/api/follows", json={"full_name": ""}).status_code == 400
    assert client.post("/api/follows", json={"full_name": "a/b/c"}).status_code == 400
    assert client.post("/api/follows", json={"full_name": 123}).status_code == 400
    assert client.post("/api/follows", json=["tiny/lib"]).status_code == 400
    assert client.post("/api/follows", json={"full_name": "tiny/lib", "as_of": "2026-08-09"}).status_code == 400
    assert fake.calls == []  # 全部被校验层拦下
    # as_of 合法定长 ISO 放行
    resp = client.post("/api/follows", json={"full_name": "tiny/lib", "as_of": "2026-08-09T08:00:00Z"})
    assert resp.status_code == 200


def test_api_follow_not_found_and_upstream_error(api):
    """GitHub 404 → API 404（三表零变化）；上游故障 → 502。"""
    client, fake, db = api
    resp = client.post("/api/follows", json={"full_name": "ghost/none"})
    assert resp.status_code == 404
    assert "ghost/none" in resp.json()["detail"]
    conn = get_conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0] == 0
    finally:
        conn.close()
    # 上游故障（限速/5xx 耗尽重试后）：502 且不入半池
    app.dependency_overrides[_github_client] = lambda: FakeRepoClient(error=GitHubError("模拟限速"))
    resp = client.post("/api/follows", json={"full_name": "tiny/lib"})
    assert resp.status_code == 502


def test_api_unfollow(api):
    """DELETE /api/follows/{owner}/{repo}：删 follows 保留跟踪；重复取消/取消不存在均幂等 200。"""
    client, _, db = api
    client.post("/api/follows", json={"full_name": "tiny/lib"})
    resp = client.delete("/api/follows/tiny/lib")
    assert resp.status_code == 200
    data = resp.json()
    assert data["followed"] is False and data["removed"] is True and data["follow_count"] == 0
    conn = get_conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0] == 0
        assert conn.execute("SELECT dead FROM repos WHERE full_name = 'tiny/lib'").fetchone()[0] == 0  # 跟踪保留
        assert conn.execute("SELECT COUNT(*) FROM star_snapshots").fetchone()[0] == 1
    finally:
        conn.close()
    # 幂等：再删一次 / 删从未关注的 / 删不在池的，都 200 且 removed=false
    assert client.delete("/api/follows/tiny/lib").json()["removed"] is False
    assert client.delete("/api/follows/ghost/none").json()["removed"] is False
    # 非法形态 400
    assert client.delete("/api/follows/oops").status_code == 400


def test_api_follow_then_shows_on_follows_page(api):
    """四格尺"能操作/能延续"链路（T-015 P6）：API 关注后 /follows 出该行、星标实心、计数徽标 1；P1 不再有关注区。"""
    client, _, _ = api
    client.post("/api/follows", json={"full_name": "tiny/lib"})
    text = client.get("/follows").text
    assert 'data-repo="tiny/lib"' in text  # P6 行渲染
    assert 'class="star on" data-repo="tiny/lib"' in text  # 星标实心态由 SSR 持久
    assert "—— 下周起有数据" in text  # 新入池首周缺席口径（na 沉组尾）
    assert '我的关注<i id="nav-follow-count">1</i>' in text  # 顶栏计数徽标 SSR 真值
    home = client.get("/").text
    assert "follows-sec" not in home  # v1.3：P1 顶部关注区已移出为独立页 P6


def test_api_follow_dead_revives(api):
    """态②走 API（评审低-3）：dead 仓复活 → 200 state=revived；dead=0、follows 有行、补一张基线。"""
    client, fake, db = api
    conn = get_conn(db)
    conn.execute(
        "INSERT INTO repos (full_name, node_id, language, topics, dead, source, created_at)"
        " VALUES ('a/dead', 'node-a/dead', 'Go', '[]', 1, 'discover', '2026-08-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars)"
        " VALUES ((SELECT id FROM repos WHERE full_name = 'a/dead'), '2026-08-01T00:00:00Z', 77)"
    )
    conn.commit()
    conn.close()
    fake.items["a/dead"] = make_repo_item("a/dead", stars=88)
    resp = client.post("/api/follows", json={"full_name": "a/dead"})
    assert resp.status_code == 200
    assert resp.json()["state"] == STATE_REVIVED
    assert fake.calls == ["a/dead"]  # 态②要拿最新元数据/星数，打一次 GitHub
    conn = get_conn(db)
    try:
        row = conn.execute("SELECT id, dead FROM repos WHERE full_name = 'a/dead'").fetchone()
        assert row["dead"] == 0
        assert conn.execute("SELECT COUNT(*) FROM follows WHERE repo_id = ?", (row["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM star_snapshots WHERE repo_id = ?", (row["id"],)).fetchone()[0] == 2
    finally:
        conn.close()


def test_api_follow_node_id_conflict_409(api):
    """node_id 撞库两情形均 409（评审低-3＋中-1）：改名旧名在库；同名删除重建 node_id 已变。"""
    client, fake, db = api
    conn = get_conn(db)
    conn.execute(
        "INSERT INTO repos (full_name, node_id, language, topics, dead, source, created_at)"
        " VALUES ('a/x', 'node-a/x', 'Go', '[]', 1, 'initial', '2026-08-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    # 情形一：改名——b/y 的 node_id 撞库内 a/x（ValueError → 409 既有映射）
    fake.items["b/y"] = make_repo_item("b/y", node_id="node-a/x")
    resp = client.post("/api/follows", json={"full_name": "b/y"})
    assert resp.status_code == 409
    assert "改名" in resp.json()["detail"]
    # 情形二：同名删除重建——a/x 的 node_id 已变（中-1 新检查）
    fake.items["a/x"] = make_repo_item("a/x", node_id="node-NEW")
    resp = client.post("/api/follows", json={"full_name": "a/x"})
    assert resp.status_code == 409
    assert "node_id 已变化" in resp.json()["detail"]
    conn = get_conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1  # 无新入池行
        assert conn.execute("SELECT dead FROM repos WHERE full_name = 'a/x'").fetchone()[0] == 1  # 未复活
    finally:
        conn.close()


def test_api_follow_bad_body_400(api):
    """请求体边界（评审低-3）：非字符串 as_of、空 body、非 JSON body 全部 400，不打 GitHub。"""
    client, fake, _ = api
    assert client.post("/api/follows", json={"full_name": "tiny/lib", "as_of": 123}).status_code == 400
    assert client.post("/api/follows").status_code == 400  # 空 body
    resp = client.post("/api/follows", content=b"not json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert fake.calls == []


# ---------- 页面路由 POST /follows/add（direct-follow §21.1~§21.4；TestClient 不跟随跳转以断言 303） ----------


def _follow_add(client, raw):
    """提交 P6 地址表单（urlencoded）；TestClient 用 follow_redirects=False 断言 303 本身。"""
    return client.post("/follows/add", data={"repo": raw}, follow_redirects=False)


def test_follows_add_joined_303_and_banner(api):
    """态③ 未入池：303 → ?added=tiny/lib&result=joined；GET 渲染结果横幅＋语言组锚点，行落入该组。

    地址段大小写混合（TINY/lib）时按输入去问 GitHub，回库与横幅用 GitHub 返回的规范名 tiny/lib。
    """
    client, fake, _ = api
    fake.items["TINY/lib"] = make_repo_item("tiny/lib", stars=66, language="Rust")
    resp = _follow_add(client, "https://github.com/TINY/lib")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/follows?added=tiny%2Flib&result=joined"
    banner, body = _get_follows(client, resp.headers["location"])
    assert banner == "已关注 tiny/lib：此前不在池内，已即时抓取基线快照并入池，下周起显示当周增量。"
    assert '<a href="#f-rust">在下方「Rust」组查看</a>' in body
    assert 'id="f-rust"' in body and 'data-repo="tiny/lib"' in body
    assert '我的关注<i id="nav-follow-count">1</i>' in body  # 顶栏计数与实际关注数一致
    assert fake.calls == ["TINY/lib"]


def test_follows_add_alive_303_and_banner(api):
    """态① 已入池 alive：303 result=alive，0 次 GitHub 调用，横幅不带"已抓取"字样。"""
    client, fake, db = api
    conn = get_conn(db)
    try:
        conn.execute(
            "INSERT INTO repos (full_name, node_id, language, topics, dead, source, created_at)"
            " VALUES ('a/pooled', 'node-a/pooled', 'Go', '[]', 0, 'initial', '2026-08-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()
    resp = _follow_add(client, "a/pooled")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/follows?added=a%2Fpooled&result=alive"
    banner, body = _get_follows(client, resp.headers["location"])
    assert banner == "已关注 a/pooled：该仓库已在跟踪池内，无需额外抓取。"
    assert '<a href="#f-go">在下方「Go」组查看</a>' in body
    assert fake.calls == []  # 已入池不消耗 GitHub 配额


def test_follows_add_revived_303_and_banner(api):
    """态② dead 复活：303 result=revived，恰好 1 次 GitHub 调用，横幅为复活口径。"""
    client, fake, db = api
    conn = get_conn(db)
    try:
        conn.execute(
            "INSERT INTO repos (full_name, node_id, language, topics, dead, source, created_at)"
            " VALUES ('a/dead', 'node-a/dead', 'Python', '[]', 1, 'discover', '2026-08-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars)"
            " VALUES ((SELECT id FROM repos WHERE full_name = 'a/dead'), '2026-08-01T00:00:00Z', 77)"
        )
        conn.commit()
    finally:
        conn.close()
    fake.items["a/dead"] = make_repo_item("a/dead", stars=88, language="Python")
    resp = _follow_add(client, "git@github.com:a/dead.git")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/follows?added=a%2Fdead&result=revived"
    banner, body = _get_follows(client, resp.headers["location"])
    assert banner == "已关注 a/dead：此前已标记失效，已复活并补一张基线快照，次日起恢复每日采集。"
    assert '<a href="#f-python">在下方「Python」组查看</a>' in body
    assert fake.calls == ["a/dead"]


def test_follows_add_already_303_and_banner(api):
    """已关注幂等：303 result=already，关注列表不出现重复行、库内零变化、0 次 GitHub 调用。"""
    client, fake, _ = api
    assert _follow_add(client, "tiny/lib").status_code == 303  # 先关注
    resp = _follow_add(client, "tiny/lib")  # 再提交同一仓库
    assert resp.status_code == 303
    assert resp.headers["location"] == "/follows?added=tiny%2Flib&result=already"
    banner, body = _get_follows(client, resp.headers["location"])
    assert banner == "tiny/lib 已经在关注中，未做重复写入。"
    assert 'class="star on" data-repo="tiny/lib"' in body  # 该行在页面里（P6 行恒已关注）
    assert body.count('data-tags="[]"') == 1  # 只有一行（重复行会多出 data-tags 属性）
    assert fake.calls == ["tiny/lib"]  # 幂等短路：第二次不打 GitHub


def test_follows_add_bare_name_accepted(api):
    """裸 owner/repo 在关注页接受（§21.2）：直接归一入池，不经过 AI。"""
    client, fake, _ = api
    resp = _follow_add(client, "tiny/lib")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/follows?added=tiny%2Flib&result=joined"
    assert fake.calls == ["tiny/lib"]


def test_follows_add_unrecognized_400_keeps_input(api):
    """400 无法识别（§21.4）：状态码 400、文案逐字、输入框回填原值、完整页面骨架仍在、不打 GitHub。"""
    client, fake, _ = api
    resp = _follow_add(client, "not a repo")
    assert resp.status_code == 400
    text = resp.text
    assert "无法识别仓库地址：not a repo。支持 https://github.com/owner/repo、git@github.com:owner/repo.git 或 owner/repo。" in text
    assert 'value="not a repo"' in text  # 回填原值（模板自动转义）
    _assert_follows_skeleton(text)  # 失败页仍是完整 P6，不是残缺片段
    assert fake.calls == []  # 解析失败不打 GitHub


@pytest.mark.parametrize(
    "raw",
    [
        "https://gitlab.com/psf/requests",  # 非 github.com 域名
        "psf",  # 只有 owner
        "https://github.com/orgs/psf",  # 保留页前缀
        "https://github.com/psf",  # 地址形态缺 repo（不得掉进裸名分支）
        "psf/requests/extra",  # 裸形态三段
        "psf/requests\ngithub.com/psf/requests",  # 多地址不做批量（§21.6）
        "psf/requests #标签",  # 添加时不打标签（§21.6）
        "",  # 空提交
    ],
)
def test_follows_add_rejected_forms_400(api, raw):
    """400 矩阵（§21.2/§21.6）：非 GitHub 域名、只有 owner、保留页前缀、三段裸名、多地址、带标签、空串。"""
    client, fake, _ = api
    resp = _follow_add(client, raw)
    assert resp.status_code == 400
    assert "无法识别仓库地址：" in resp.text
    assert fake.calls == []


def test_follows_add_long_input_preserved_300(api):
    """浏览器可达的 300 字输入：横幅与输入框都按原值回显（§21.4「保留输入框原值」，不截断）。"""
    client, _, _ = api
    raw = "x" * 300
    resp = _follow_add(client, raw)
    assert resp.status_code == 400
    assert f"无法识别仓库地址：{raw}。" in resp.text
    assert f'value="{raw}"' in resp.text


def test_follows_add_oversized_body_rejected_without_echo(api):
    """防御分支：请求体超过 8192 字节 → 400 固定短文案，不回显超大原文（防大字段灌页面）。"""
    client, _, _ = api
    raw = "x" * 9000
    resp = _follow_add(client, raw)
    assert resp.status_code == 400
    assert "输入过长（超过 8192 字节）" in resp.text
    assert raw not in resp.text
    assert 'value=""' in resp.text


def test_declared_too_large_guard_edges():
    """Content-Length 预判边界（R3 F3）：超限/正常、非数字/负数、4301 位数字都不抛异常（不会变 500）。"""
    assert _declared_too_large("8192") is False
    assert _declared_too_large("8193") is True
    assert _declared_too_large("9" * 10) is True
    assert _declared_too_large("9" * 4301) is False  # 位数守卫：不调 int()，避免 Python 3.11 的 4300 位上限抛错
    assert _declared_too_large("abc") is False
    assert _declared_too_large("-1") is False
    assert _declared_too_large("") is False


def test_follows_add_error_echo_escaped(api):
    """400 回显转义（§21.4）：输入含 HTML 标签时只以转义文本进页面，不注入 DOM（Jinja 自动转义）。"""
    client, _, _ = api
    raw = "<script>alert(1)</script>"
    resp = _follow_add(client, raw)
    assert resp.status_code == 400
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text


def test_follows_add_cross_origin_rejected(api):
    """CSRF 防护（design 决策 8）：跨站 Origin 的 POST 403 且不写库；同源/可信反代 X-Forwarded-Host 正常 303。"""
    client, fake, _ = api
    cross = client.post(
        "/follows/add", data={"repo": "tiny/lib"}, headers={"Origin": "https://evil.example"}, follow_redirects=False
    )
    assert cross.status_code == 403
    assert fake.calls == []  # 未进写路径
    # 改写 Host 的反代场景：Host 是回环地址，Origin 的公网主机名由 X-Forwarded-Host 兜底
    proxied = client.post(
        "/follows/add",
        data={"repo": "tiny/lib"},
        headers={
            "Origin": "https://radar.example.com",
            "Host": "127.0.0.1:8000",
            "X-Forwarded-Host": "radar.example.com",
        },
        follow_redirects=False,
    )
    assert proxied.status_code == 303
    assert fake.calls == ["tiny/lib"]
    # 同源（TestClient 默认 Host=testserver）
    same = client.post(
        "/follows/add", data={"repo": "tiny/lib"}, headers={"Origin": "http://testserver"}, follow_redirects=False
    )
    assert same.status_code == 303
    assert fake.calls == ["tiny/lib"]  # 幂等短路，未重复打 GitHub


def test_follows_add_not_found_404(api):
    """404 仓库不存在（§21.4）：同页渲染、状态码 404、输入回填、页面骨架完整、不打库。"""
    client, fake, db = api
    resp = _follow_add(client, "ghost/none")
    assert resp.status_code == 404
    assert "GitHub 上找不到仓库 ghost/none（不存在/已删除/转私有）" in resp.text
    assert 'value="ghost/none"' in resp.text
    _assert_follows_skeleton(resp.text)
    conn = get_conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0] == 0
    finally:
        conn.close()
    assert fake.calls == ["ghost/none"]


def test_follows_add_node_id_conflict_409(api):
    """409 node_id 撞库（改名/同名重建，§21.6）：既有文案、库内 full_name 不改写、无归并、无新行。"""
    client, fake, db = api
    conn = get_conn(db)
    try:
        conn.execute(
            "INSERT INTO repos (full_name, node_id, language, topics, dead, source, created_at)"
            " VALUES ('old/name', 'node-old/name', 'Go', '[]', 0, 'initial', '2026-08-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()
    fake.items["renamed/name"] = make_repo_item("renamed/name", node_id="node-old/name")
    resp = _follow_add(client, "https://github.com/renamed/name")
    assert resp.status_code == 409
    assert (
        "仓库 renamed/name 的 node_id 与库内已有记录冲突（仓库可能已改名，旧名仍在库），暂未自动归并" in resp.text
    )  # 既有文案完整锁定（不只 "改名" 子串）
    assert 'value="https://github.com/renamed/name"' in resp.text  # 输入框保留原值（原样，未被归一覆盖）
    _assert_follows_skeleton(resp.text)
    conn = get_conn(db)
    try:
        names = [r[0] for r in conn.execute("SELECT full_name FROM repos")]
        assert names == ["old/name"]  # 未改写库内 full_name、未自动归并
        assert conn.execute("SELECT COUNT(*) FROM follows").fetchone()[0] == 0
    finally:
        conn.close()


def test_follows_add_upstream_error_502(api):
    """502 上游请求失败（§21.4）：同页渲染 `GitHub 请求失败：{原因}`、状态码 502、输入保留、三表零变化。"""
    client, _, db = api
    app.dependency_overrides[_github_client] = lambda: FakeRepoClient(error=GitHubError("模拟限速"))
    resp = _follow_add(client, "tiny/lib")
    assert resp.status_code == 502
    assert "GitHub 请求失败：模拟限速" in resp.text
    assert 'value="tiny/lib"' in resp.text
    _assert_follows_skeleton(resp.text)
    conn = get_conn(db)
    try:
        assert _counts(conn) == (0, 0, 0)
    finally:
        conn.close()


def test_follows_add_auth_error_500(api):
    """500 token 配置问题（§21.4）：沿用既有 GitHub token 文案原样透传、状态码 500、输入保留。"""
    client, _, _ = api
    app.dependency_overrides[_github_client] = lambda: FakeRepoClient(
        error=GitHubAuthError("GitHub token 为空：请在项目根 .env 配置 GITHUB_TOKEN")
    )
    resp = _follow_add(client, "tiny/lib")
    assert resp.status_code == 500
    assert "GitHub token 为空：请在项目根 .env 配置 GITHUB_TOKEN" in resp.text
    assert 'value="tiny/lib"' in resp.text
    _assert_follows_skeleton(resp.text)


def test_follows_page_empty_shows_add_form(tmp_path, monkeypatch):
    """空关注页显示添加入口与提示行（§21.1）：无 JS 也能提交（原生表单）；横幅区此刻无内容。"""
    db = tmp_path / "empty.db"
    init_db(db)
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/follows")
    assert resp.status_code == 200
    text = resp.text
    assert 'class="search-box follow-add" method="post" action="/follows/add"' in text
    assert 'name="repo"' in text and 'maxlength="300"' in text and "required" in text
    assert 'id="follow-add-input"' in text and 'value=""' in text  # GET 不回填
    assert "支持 github.com 链接、git@github.com:owner/repo.git 与 owner/repo；已关注的仓库不会重复写入。" in text
    assert _banner_text(text) is None  # 无参数 → 不渲染横幅
    assert "还没有关注任何项目：在上方粘贴 GitHub 仓库地址直接添加" in text  # 空态与入口并存


@pytest.mark.parametrize(
    "query",
    [
        "?added=tiny/lib&result=already",  # 该仓库未关注：查不到关注集记录 → 不渲染
        "?added=ghost/none&result=joined",  # 库内无此仓库
        "?added=not%20a%20repo&result=joined",  # added 形态非法
        "?added=tiny/lib&result=%3Cscript%3E",  # result 白名单外
        "?added=tiny/lib&result=",  # result 空
        "?result=joined",  # 只有 result
        "?added=tiny/lib",  # 只有 added
        "?added=%3Cimg%20src%3Dx%20onerror%3D1%3E&result=already",  # 伪造 added（注入串）
    ],
)
def test_follows_page_banner_whitelist_rejects_forged_params(api, query):
    """PRG 参数白名单（design 决策 6）：added 必须过形态校验且在关注集内、result 必须在四值内，否则不渲染横幅。"""
    client, _, _ = api
    resp = client.get(f"/follows{query}")
    assert resp.status_code == 200
    assert _banner_text(resp.text) is None
    assert "<img src=x onerror=1>" not in resp.text  # 任意输入不进页面


def test_follows_page_banner_after_unfollow_not_shown(api):
    """过期参数：关注后取消关注，再带原 added/result 访问 → 横幅不渲染（查库已查不到）。"""
    client, _, _ = api
    assert _follow_add(client, "tiny/lib").status_code == 303
    assert client.delete("/api/follows/tiny/lib").json()["removed"] is True
    resp = client.get("/follows?added=tiny%2Flib&result=already")
    assert resp.status_code == 200
    assert _banner_text(resp.text) is None


def test_follows_page_banner_other_language_group(api):
    """无语言（language 为空）的仓库落「其它语言」组：锚点 #f-other、组标签「其它语言」（§21.4）。"""
    client, _, db = api
    conn = get_conn(db)
    try:
        conn.execute(
            "INSERT INTO repos (full_name, node_id, language, topics, dead, source, created_at)"
            " VALUES ('a/nolang', 'node-a/nolang', NULL, '[]', 0, 'initial', '2026-08-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()
    resp = _follow_add(client, "a/nolang")
    assert resp.status_code == 303
    banner, body = _get_follows(client, resp.headers["location"])
    assert banner == "已关注 a/nolang：该仓库已在跟踪池内，无需额外抓取。"
    assert '<a href="#f-other">在下方「其它语言」组查看</a>' in body
    assert 'id="f-other"' in body
