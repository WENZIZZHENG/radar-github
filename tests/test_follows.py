"""关注（T-009，决策 9 动态入池）测试：假 GitHub client + tmp_path 真实 sqlite 文件，全程离线，禁真打 API。

覆盖任务书钉死口径：关注三态各自的库内状态、幂等重复关注、取消保留跟踪、取消不存在关注、
关注 API 的 400/404/502/409 与幂等；未入池抓取一律 mock（FakeRepoClient）。
不引 pytest-asyncio（不加新依赖）：异步用例统一 asyncio.run 驱动，与 test_github/test_snapshot 同口径。
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.collector.github import GitHubError, GitHubNotFoundError
from app.db import get_conn, init_db
from app.follows import STATE_ALIVE, STATE_JOINED, STATE_REVIVED, follow_repo, unfollow_repo
from app.main import app
from app.web.routes import _github_client

NOW = "2026-08-09T08:00:00Z"  # 关注/基线共享的固定时间戳（UTC 定长硬约定）


def make_repo_item(full_name, *, stars=42, description="demo", language="Go", topics=("cli",), node_id=None):
    """造一条 REST /repos/{full_name} 响应（字段对齐入库子集，与 Search item 同构）。"""
    return {
        "full_name": full_name,
        "node_id": node_id or f"node-{full_name}",
        "description": description,
        "language": language,
        "topics": list(topics),
        "stargazers_count": stars,
    }


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
