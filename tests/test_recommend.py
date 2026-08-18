"""T-017 推荐语 API 测试：TestClient 打真实 app（lifespan 会跑 init_db），tmp_path 独立库 + 调度关闭。

口径钉死（《交互流程说明》§8.3/§8.4）：单个 /api/recommend 强制重生（按当前页维度覆盖同维度当期行）；
批量 /api/recommend-missing 后台任务（POST 202 立即返回＋/status 轮询进度，与翻译批量并列独立不共用）
只补范围内缺失（S = 三口径榜 ∪ 关注集 × 适用维度）；两者不触碰 repos 翻译字段。
AI 调用全 mock（单个走 dependency_overrides、批量 worker 走 monkeypatch 模块工厂 _make_ai_client/
_make_github_client，同 tests/test_follows.py 手法）；key 未配置用例不 override、走真 DeepSeekClient
使用点拦截（空 key 不发请求直接 DeepSeekAuthError）。全程离线，禁止真实 API（T-016 事故零容忍）。
"""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.web.routes as routes
from app.ai import DeepSeekError
from app.db import get_conn, init_db
from app.main import app
from app.web.routes import _ai_client, _github_client


def _current_week_label() -> str:
    """真实今天所在 ISO 周标签（与 routes 的 _week_label 同口径）：批量/单个周维度断言用它。"""
    iso = datetime.now(timezone.utc).date().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _add_repo(conn, name, *, description_en, description_zh=None, stars=1000):
    """插一个池内仓库＋两端快照（周榜出席；total 榜出席），返回 repo_id。

    快照相对"今天"构造（两端跨 7 天，与 test_api_recommend_week_dimension_writes_current_week 同手法）：
    周维度恒出席（delta=100）、季维度恒缺席进新区——断言不随真实日期漂移（固定日期快照在运行日
    距其超过 9 天后会滑出周窗口，2026-08-18 实测变红后改为相对造法）。
    """
    now = datetime.now(timezone.utc)
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at)"
        " VALUES (?, ?, ?, ?, 'Python', '[]', 0, 'test', ?)",
        (name, f"node-{name}", description_en, description_zh, (now - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    repo_id = cur.lastrowid
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
        (repo_id, (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"), stars - 100),
    )
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
        (repo_id, now.strftime("%Y-%m-%dT%H:%M:%SZ"), stars),
    )
    conn.commit()
    return repo_id


class FakeAiClient:
    """假 DeepSeek client：recommend 记录全部入参回显"推荐语-<full_name>-<dimension>"；fail_for 按
    full_name 注入单条失败（不区分维度）；aclose 与真 client 同签名（批量 worker finally 关闭）。"""

    def __init__(self, *, fail_for=()):
        self.fail_for = set(fail_for)
        self.calls: list[dict] = []

    async def recommend(self, *, full_name, description, language, categories, dimension, delta=None, stars=None, readme=None, pool_days=None):
        self.calls.append(
            {
                "full_name": full_name,
                "dimension": dimension,
                "delta": delta,
                "stars": stars,
                "readme": readme,
                "pool_days": pool_days,  # T-018：新区仓入池语境（主榜仓 None）
            }
        )
        if full_name in self.fail_for:
            raise DeepSeekError("模拟推荐语失败")
        return f"推荐语-{full_name}-{dimension}"

    async def aclose(self) -> None:
        pass


class FakeGitHubClient:
    """假 GitHub client：fetch_readme 按 full_name 返回 (text, sha)；auth_for 注入账户类错误。"""

    def __init__(self, *, readmes=None, auth_for=()):
        self.readmes = readmes or {}
        self.auth_for = set(auth_for)
        self.calls: list[str] = []

    async def fetch_readme(self, full_name: str) -> tuple[str | None, str | None]:
        self.calls.append(full_name)
        if full_name in self.auth_for:
            from app.collector.github import GitHubAuthError

            raise GitHubAuthError("GitHub token 无效或已过期")
        return self.readmes.get(full_name, (None, None))

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clear_overrides():
    """任何用例后清依赖注入残留（多用例共进程，防串）。"""
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_rec_batch_state():
    """批量推荐后台任务状态/任务引用是模块级单例，跨用例残留：每个用例前复位并取消可能遗留的任务。"""
    task = routes._rec_batch_task
    if task is not None and not task.done():
        task.cancel()
    with routes._rec_batch_state_lock:
        routes._rec_batch_state.update(
            running=False, recommended=0, failed=0, total=0, finished=False, error=None
        )
    yield


def _make_client(tmp_path, monkeypatch, *, seed=None, override_ai=None, override_gh=None):
    db = tmp_path / "rec.db"
    init_db(db)
    conn = get_conn(db)
    if seed is not None:
        seed(conn)
    conn.close()
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    if override_ai is not None:
        app.dependency_overrides[_ai_client] = override_ai
    if override_gh is not None:
        app.dependency_overrides[_github_client] = override_gh
    return TestClient(app)


def _rec_map(db):
    conn = get_conn(db)
    try:
        return {
            (r["dimension"], r["period_label"]): r["text"]
            for r in conn.execute("SELECT dimension, period_label, text FROM recommendations WHERE repo_id = 1")
        }
    finally:
        conn.close()


def _wait_rec_batch_finished(client, *, timeout=5.0):
    """真等待轮询 /status 直到 finished（每次 0.15s，总超时 5s 防挂死）；返回最终 state dict。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/api/recommend-missing/status").json()
        if state["finished"]:
            return state
        time.sleep(0.15)
    raise AssertionError(f"批量推荐任务 {timeout}s 内未完成，最后状态: {state}")


def _seed_listed(conn):
    """一个上榜仓（周榜出席、季榜新区）：批量补缺 total 应为 3（week 当周 ＋ quarter 新区 ＋ total/all）。"""
    _add_repo(conn, "a/one", description_en="english text")


# ---------- POST /api/recommend：单个强制重生 ----------


def test_api_recommend_force_regenerate_overwrites(tmp_path, monkeypatch):
    """强制重生：已有推荐语也被新文本覆盖（§8.4 单个＝强制）；README 输入透传；total 行更新 readme_sha；
    响应形态完整；不触碰 repos 翻译字段。"""

    def seed(conn):
        repo_id = _add_repo(conn, "a/one", description_en="english text")
        conn.execute(
            "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
            " VALUES (?, 'total', 'all', '旧文本', NULL, '2026-W32')",
            (repo_id,),
        )
        conn.commit()

    fake = FakeAiClient()
    fake_gh = FakeGitHubClient(readmes={"a/one": ("# One", "sha-new")})
    with _make_client(tmp_path, monkeypatch, seed=seed, override_ai=lambda: fake, override_gh=lambda: fake_gh) as client:
        resp = client.post(
            "/api/recommend", json={"full_name": "a/one", "dimension": "total", "period_label": "all"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "full_name": "a/one",
        "recommended": True,
        "text": "推荐语-a/one-total",
        "dimension": "total",
        "period_label": "all",
        "reason_label": "总星榜推荐语",  # 块标题维度标识（2026-08-12 复验反馈），前端局部替换同构
    }
    assert fake.calls[0]["readme"] == "# One"  # README 输入透传
    assert _rec_map(tmp_path / "rec.db") == {("total", "all"): "推荐语-a/one-total"}  # 覆盖写库
    conn = get_conn(tmp_path / "rec.db")
    try:
        row = conn.execute(
            "SELECT readme_sha FROM recommendations WHERE dimension = 'total'"
        ).fetchone()
        assert row["readme_sha"] == "sha-new"  # total 行更新 sha（防次日 ensure 误判变更）
        assert conn.execute("SELECT description_zh FROM repos WHERE full_name = 'a/one'").fetchone()[0] is None
    finally:
        conn.close()


def test_api_recommend_week_dimension_writes_current_week(tmp_path, monkeypatch):
    """周维度强制重生：写入 (repo_id, 'week', 当周标签)；增量语境按当期窗口换算（出席带 delta）。"""
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)

    def seed(conn):
        # 快照相对"今天"构造（7 天窗口两端）：周维度出席，delta 恒 100，不随真实日期漂移
        cur = conn.execute(
            "INSERT INTO repos (full_name, node_id, description_en, language, topics, dead, source, created_at)"
            " VALUES (?, ?, ?, 'Python', '[]', 0, 'test', ?)",
            ("a/one", "node-a/one", "english text", (now - __import__("datetime").timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        repo_id = cur.lastrowid
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, (now - __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"), 900),
        )
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, now.strftime("%Y-%m-%dT%H:%M:%SZ"), 1000),
        )
        conn.commit()

    fake = FakeAiClient()
    week_label = _current_week_label()
    with _make_client(tmp_path, monkeypatch, seed=seed, override_ai=lambda: fake) as client:
        resp = client.post("/api/recommend", json={"full_name": "a/one", "dimension": "week", "period_label": week_label})
    assert resp.status_code == 200
    assert resp.json()["period_label"] == week_label
    call = fake.calls[0]
    assert call["dimension"] == "week"
    assert call["delta"] == 100  # 7 天窗口两端：delta=100
    conn = get_conn(tmp_path / "rec.db")
    try:
        row = conn.execute(
            "SELECT dimension, period_label, text FROM recommendations WHERE repo_id = 1"
        ).fetchone()
        assert (row["dimension"], row["period_label"], row["text"]) == ("week", week_label, "推荐语-a/one-week")
    finally:
        conn.close()


def test_api_recommend_bad_params_400(tmp_path, monkeypatch):
    """入参校验（fail-loud）：dimension 非法 / period_label 格式错 / total 非 'all' / full_name 形态非法 → 400。"""
    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=_seed_listed, override_ai=lambda: fake) as client:
        base = {"full_name": "a/one"}
        assert client.post("/api/recommend", json={**base, "dimension": "month"}).status_code == 400
        assert client.post("/api/recommend", json={**base, "dimension": "week", "period_label": "oops"}).status_code == 400
        assert client.post("/api/recommend", json={**base, "dimension": "quarter", "period_label": "2026-W32"}).status_code == 400
        assert client.post("/api/recommend", json={**base, "dimension": "total", "period_label": "2026-W32"}).status_code == 400
        assert client.post("/api/recommend", json={"full_name": "bad", "dimension": "week", "period_label": "2026-W32"}).status_code == 400
        assert client.post("/api/recommend", content="not json").status_code == 400


def test_api_recommend_repo_missing_404(tmp_path, monkeypatch):
    """仓库不在跟踪池 → 404。"""
    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, override_ai=lambda: fake) as client:
        resp = client.post("/api/recommend", json={"full_name": "ghost/none", "dimension": "total", "period_label": "all"})
    assert resp.status_code == 404
    assert "ghost/none" in resp.json()["detail"]


def test_api_recommend_failure_keeps_old_text(tmp_path, monkeypatch):
    """AI 失败 → 502 且旧文本保留（§8.3：失败分支旧文本保留不变，按钮恢复）。"""
    fake = FakeAiClient(fail_for={"a/one"})
    with _make_client(tmp_path, monkeypatch, seed=_seed_listed, override_ai=lambda: fake) as client:
        resp = client.post("/api/recommend", json={"full_name": "a/one", "dimension": "total", "period_label": "all"})
    assert resp.status_code == 502
    assert _rec_map(tmp_path / "rec.db") == {}  # 未写库


def test_api_recommend_no_key_500(tmp_path, monkeypatch):
    """key 未配置：真 client 使用点拦截 → 500（detail 带修复指引；§8.3 前端 toast"未配置 DeepSeek API key"）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    with _make_client(tmp_path, monkeypatch, seed=_seed_listed) as client:  # 不 override：走真 DeepSeekClient
        resp = client.post("/api/recommend", json={"full_name": "a/one", "dimension": "total", "period_label": "all"})
    assert resp.status_code == 500
    assert "DEEPSEEK_API_KEY" in resp.json()["detail"]
    assert _rec_map(tmp_path / "rec.db") == {}  # 不写库


def test_api_recommend_readme_failure_degrades(tmp_path, monkeypatch):
    """README 拉取失败 → 推荐语照常生成（退化元数据输入），total 行 readme_sha=NULL，不阻塞。"""
    from app.collector.github import GitHubError

    class BadGh(FakeGitHubClient):
        async def fetch_readme(self, full_name):
            self.calls.append(full_name)
            raise GitHubError("模拟 README 拉取失败")

    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=_seed_listed, override_ai=lambda: fake, override_gh=lambda: BadGh()) as client:
        resp = client.post("/api/recommend", json={"full_name": "a/one", "dimension": "total", "period_label": "all"})
    assert resp.status_code == 200
    assert fake.calls[0]["readme"] is None
    conn = get_conn(tmp_path / "rec.db")
    try:
        assert conn.execute("SELECT readme_sha FROM recommendations WHERE dimension = 'total'").fetchone()[0] is None
    finally:
        conn.close()


def test_api_recommend_readme_failure_keeps_sha(tmp_path, monkeypatch):
    """F2-1 修复：total 维度单个重生＋README 拉取失败 → 文本正常更新（退化输入），但 readme_sha
    保留旧值（不清指纹，防次日 ensure 把手动重生误判为 README 变更再触发重生）。"""
    from app.collector.github import GitHubError

    class BadGh(FakeGitHubClient):
        async def fetch_readme(self, full_name):
            self.calls.append(full_name)
            raise GitHubError("模拟 README 拉取失败")

    def seed(conn):
        repo_id = _add_repo(conn, "a/one", description_en="english text")
        conn.execute(
            "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
            " VALUES (?, 'total', 'all', '旧文本', 'sha-old', '2026-W32')",
            (repo_id,),
        )
        conn.commit()

    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=seed, override_ai=lambda: fake, override_gh=lambda: BadGh()) as client:
        resp = client.post("/api/recommend", json={"full_name": "a/one", "dimension": "total", "period_label": "all"})
    assert resp.status_code == 200
    assert fake.calls[0]["readme"] is None  # 退化元数据输入
    conn = get_conn(tmp_path / "rec.db")
    try:
        row = conn.execute(
            "SELECT text, readme_sha FROM recommendations WHERE dimension = 'total'"
        ).fetchone()
        assert row["text"] == "推荐语-a/one-total"  # 文本已更新
        assert row["readme_sha"] == "sha-old"  # 指纹保留旧值
    finally:
        conn.close()


# ---------- POST /api/recommend-missing：批量只补缺失 ＋ running 409 ----------


def test_api_recommend_missing_only_fills_missing(tmp_path, monkeypatch):
    """批量只补缺失（后台任务形态）：202 立即返回＋/status 轮询到 finished；已存在的维度行不覆盖；
    缺 quarter（T-018 新区）＋total 两维度 → total=2；不触碰 repos 翻译字段。"""
    week_label = _current_week_label()

    def seed(conn):
        repo_id = _add_repo(conn, "a/one", description_en="english text")
        conn.execute(
            "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
            " VALUES (?, 'week', ?, '已有周文本', NULL, ?)",
            (repo_id, week_label, week_label),
        )
        conn.commit()

    fake = FakeAiClient()
    fake_gh = FakeGitHubClient(readmes={"a/one": ("# One", "sha-1")})
    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        monkeypatch.setattr(routes, "_make_ai_client", lambda: fake)
        monkeypatch.setattr(routes, "_make_github_client", lambda: fake_gh)
        resp = client.post("/api/recommend-missing")
        assert resp.status_code == 202
        assert resp.json() == {"started": True, "total": 2}  # 周行已存在：缺 quarter（新区）＋ total/all
        state = _wait_rec_batch_finished(client)
        assert state["running"] is False and state["finished"] is True and state["error"] is None
        assert state["recommended"] == 2 and state["failed"] == 0
    assert [c["dimension"] for c in fake.calls] == ["quarter", "total"]  # 只补缺失维度
    rec = _rec_map(tmp_path / "rec.db")
    assert rec[("week", week_label)] == "已有周文本"  # 已存在的行不覆盖
    assert rec[("total", "all")] == "推荐语-a/one-total"


def test_api_recommend_missing_partial_failure_degrades(tmp_path, monkeypatch):
    """批量中途单条失败（后台任务）：不炸、失败计入 failed 整批继续、库内不留半成品（降级链同 ensure）。"""

    def seed(conn):
        _add_repo(conn, "a/bad", description_en="boom text")
        _add_repo(conn, "a/good", description_en="good text")

    fake = FakeAiClient(fail_for={"a/bad"})
    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        monkeypatch.setattr(routes, "_make_ai_client", lambda: fake)
        monkeypatch.setattr(routes, "_make_github_client", lambda: FakeGitHubClient())
        resp = client.post("/api/recommend-missing")
        assert resp.status_code == 202
        assert resp.json() == {"started": True, "total": 6}  # T-018：2 仓 ×（week＋quarter 新区＋total）
        state = _wait_rec_batch_finished(client)
        assert state["finished"] is True and state["error"] is None
        assert state["recommended"] == 3 and state["failed"] == 3  # a/bad 三维度失败，a/good 三维度成功
    conn = get_conn(tmp_path / "rec.db")
    try:
        bad_rows = conn.execute(
            "SELECT COUNT(*) FROM recommendations c JOIN repos r ON r.id = c.repo_id WHERE r.full_name = 'a/bad'"
        ).fetchone()[0]
        assert bad_rows == 0  # 失败不落库：次日/再触发自然补缺
    finally:
        conn.close()


def test_api_recommend_missing_inflight_409(tmp_path, monkeypatch):
    """running 态 409（§8.3 toast"补齐进行中…"）：挂起窗口内重复 POST → 409；任务完成后状态收敛。"""
    entered = threading.Event()

    class BlockingAi(FakeAiClient):
        async def recommend(self, **kwargs):
            entered.set()
            await asyncio.sleep(0.5)
            return f"推荐语-{kwargs['full_name']}-{kwargs['dimension']}"

    with _make_client(tmp_path, monkeypatch, seed=_seed_listed) as client:
        monkeypatch.setattr(routes, "_make_ai_client", lambda: BlockingAi())
        monkeypatch.setattr(routes, "_make_github_client", lambda: FakeGitHubClient())
        resp1 = client.post("/api/recommend-missing")
        assert resp1.status_code == 202
        assert entered.wait(timeout=5)  # worker 已进入生成中（in-flight 窗口）
        resp2 = client.post("/api/recommend-missing")
        assert resp2.status_code == 409
        state = _wait_rec_batch_finished(client)
        assert state["finished"] is True and state["error"] is None
        assert state["recommended"] == 3 and state["failed"] == 0  # T-018：week＋quarter 新区＋total


def test_api_recommend_missing_no_key_auth_error(tmp_path, monkeypatch):
    """key 未配置：worker 内真 client 使用点拦截（不发请求）→ finished 且 error=="auth"
    （§8.3 前端 toast"未配置 DeepSeek API key"）；不写库。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    with _make_client(tmp_path, monkeypatch, seed=_seed_listed) as client:  # 不 monkeypatch 工厂：真 client 空 key 拦截
        resp = client.post("/api/recommend-missing")
        assert resp.status_code == 202
        state = _wait_rec_batch_finished(client)
        assert state["finished"] is True and state["error"] == "auth"
        assert state["recommended"] == 0 and state["failed"] == 0
    assert _rec_map(tmp_path / "rec.db") == {}


def test_api_recommend_missing_worker_conn_crash_unknown(tmp_path, monkeypatch):
    """worker 启动即炸（get_conn 抛异常）：异常落 error="unknown"，终态收敛（running=False/finished=True），
    任务无未捕获异常冒到事件循环。"""
    real_get_conn = routes.get_conn
    call_count = {"n": 0}

    def flaky_conn(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] > 1:  # 第 1 次是 POST 侧（请求内），第 2 次是 worker（后台任务内）——worker 这次炸
            raise RuntimeError("模拟 worker get_conn 崩溃")
        return real_get_conn(*args, **kwargs)

    with _make_client(tmp_path, monkeypatch, seed=_seed_listed) as client:
        monkeypatch.setattr(routes, "get_conn", flaky_conn)
        monkeypatch.setattr(routes, "_make_ai_client", lambda: FakeAiClient())
        monkeypatch.setattr(routes, "_make_github_client", lambda: FakeGitHubClient())
        resp = client.post("/api/recommend-missing")
        assert resp.status_code == 202
        state = _wait_rec_batch_finished(client)
        assert state["finished"] is True and state["running"] is False
        assert state["error"] == "unknown"
        assert state["recommended"] == 0 and state["failed"] == 0
    task = routes._rec_batch_task
    assert task.done() and task.exception() is None  # 无未捕获异常冒到事件循环


def test_api_recommend_missing_second_post_resets_counters(tmp_path, monkeypatch):
    """任务完成后再次 POST：202 且计数重置为新一轮（§8.2 下次 POST 重置）——新一轮 status 的
    total/recommended 不含上一轮数字。"""
    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=_seed_listed) as client:
        monkeypatch.setattr(routes, "_make_ai_client", lambda: fake)
        monkeypatch.setattr(routes, "_make_github_client", lambda: FakeGitHubClient())
        resp1 = client.post("/api/recommend-missing")
        assert resp1.status_code == 202
        assert resp1.json() == {"started": True, "total": 3}  # T-018：week＋quarter 新区＋total
        state1 = _wait_rec_batch_finished(client)
        assert state1["recommended"] == 3 and state1["failed"] == 0
        # 第二轮：全部已生成 → 新一轮 total=0（若未重置会残留上一轮数字）
        resp2 = client.post("/api/recommend-missing")
        assert resp2.status_code == 202
        assert resp2.json() == {"started": True, "total": 0}
        state2 = _wait_rec_batch_finished(client)
        assert state2["total"] == 0 and state2["recommended"] == 0 and state2["failed"] == 0
        assert state2["error"] is None and state2["finished"] is True and state2["running"] is False
    assert len(fake.calls) == 3  # 第二轮无缺失，不再调用


def test_recommend_missing_status_default(tmp_path, monkeypatch):
    """无任务史：GET /status 返回全零/false/None 默认态（不依赖 AI/GitHub client）。"""
    with _make_client(tmp_path, monkeypatch) as client:
        assert client.get("/api/recommend-missing/status").json() == {
            "running": False,
            "recommended": 0,
            "failed": 0,
            "total": 0,
            "finished": False,
            "error": None,
        }


# ---------- SSR 形态：行内推荐按钮按态 ＋ 顶栏批量按钮 ----------


def test_row_recommend_button_ssr_states(tmp_path, monkeypatch):
    """行内按钮按态（§8.2）：无当前页维度推荐语"生成推荐语"＋data-has-reason=0；已有"重新生成"＋
    data-has-reason=1；顶栏批量按钮与翻译按钮并列就位。"""

    def seed(conn):
        repo_id = _add_repo(conn, "a/en", description_en="english text")
        _add_repo(conn, "a/zh", description_en="english too")
        conn.execute(
            "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
            " VALUES (?, 'total', 'all', '已有推荐语', NULL, '2026-W32')",
            (repo_id,),
        )
        conn.execute(
            "INSERT INTO tags (repo_id, tag) VALUES ((SELECT id FROM repos WHERE full_name = 'a/en'), '选型观察')"
        )
        conn.commit()

    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        text = client.get("/total?board=all").text  # T-026：board=all 全量语境（a/en/a/zh 无语言 → 其它语言榜）
    assert 'class="recommend-btn" data-repo="a/en" data-dim="total" data-period-label="all" data-has-reason="1">重新生成</button>' in text  # a/en 有 total 行
    assert 'class="recommend-btn" data-repo="a/zh" data-dim="total" data-period-label="all" data-has-reason="0">生成推荐语</button>' in text  # a/zh 无 → 按态
    header_section = text.split("</header>", 1)[0]
    assert '<button class="translate-all recommend-all" id="recommend-all">补齐推荐语</button>' in header_section
    assert '<button class="translate-all" id="translate-all">补译全部缺失</button>' in header_section  # 与翻译批量并列
    # 标签结果页不渲染行内推荐按钮（§8.2 只在周/季/总星/关注四页出现）
    text_tags = client.get("/tags/%E9%80%89%E5%9E%8B%E8%A7%82%E5%AF%9F").text
    assert 'class="recommend-btn"' not in text_tags
