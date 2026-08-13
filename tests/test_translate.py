"""T-016 翻译 API 测试：TestClient 打真实 app（lifespan 会跑 init_db），tmp_path 独立库 + 调度关闭。

口径钉死（《交互流程说明》§7.3/§7.4）：单个 /api/translate 强制重译（覆盖现值）；
批量 /api/translate-missing 后台任务（POST 202 立即返回＋/status 轮询进度）只补 NULL＋running 409（重复触发）；
两者不触碰 recommendations 表。AI 调用全 mock（单个走 dependency_overrides、批量 worker 走 monkeypatch
模块工厂 _make_ai_client，同 tests/test_follows.py 手法）；key 未配置用例不 override、走真 DeepSeekClient
使用点拦截（空 key 不发请求直接 DeepSeekAuthError）。
"""

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

import app.web.routes as routes
from app.ai import DeepSeekError
from app.db import get_conn, init_db
from app.main import app
from app.web.routes import _ai_client


def _add_repo(conn, name, *, description_en, description_zh=None, snapshots=(("2026-08-09T00:00:00Z", 1000),)):
    """插一个池内仓库＋一张快照（total 榜展示需要；快照日与 created_at 同口径足够），返回 repo_id。"""
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at)"
        " VALUES (?, ?, ?, ?, 'Python', '[]', 0, 'test', '2026-07-01T00:00:00Z')",
        (name, f"node-{name}", description_en, description_zh),
    )
    repo_id = cur.lastrowid
    for captured_at, stars in snapshots:
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, captured_at, stars),
        )
    conn.commit()
    return repo_id


class FakeAiClient:
    """假 DeepSeek client：translate 回显"译文-<原文>"；fail_for 按原文文本注入单条失败（同 test_ai 手法）。"""

    def __init__(self, *, fail_for=()):
        self.fail_for = set(fail_for)
        self.calls: list[str] = []

    async def translate(self, text: str) -> str:
        self.calls.append(text)
        if text in self.fail_for:
            raise DeepSeekError("模拟翻译失败")
        return f"译文-{text}"

    async def aclose(self) -> None:
        pass  # 与真 client 同签名（批量 worker finally 会关闭自造 client）


class BlockingAiClient(FakeAiClient):
    """挂起版假 client：首次 translate 置 entered 后 await sleep 制造 in-flight 窗口。

    注意必须用 await 挂起（asyncio.sleep）而不是同步等待：同步等待会阻塞 TestClient 的事件循环，
    使第二个请求排队串行执行，in-flight 场景无法复现（锁拿不到并发窗口）。
    """

    def __init__(self, entered: threading.Event, hold_seconds: float = 0.5):
        super().__init__()
        self.entered = entered
        self.hold_seconds = hold_seconds

    async def translate(self, text: str) -> str:
        self.entered.set()
        await asyncio.sleep(self.hold_seconds)
        return f"译文-{text}"


@pytest.fixture(autouse=True)
def _clear_overrides():
    """任何用例后清依赖注入残留（多用例共进程，防串）。"""
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_batch_state():
    """批量后台任务状态/任务引用是模块级单例，跨用例残留：每个用例前复位并取消可能遗留的任务，防串。"""
    task = routes._batch_task
    if task is not None and not task.done():
        task.cancel()
    with routes._batch_state_lock:
        routes._batch_state.update(running=False, translated=0, failed=0, total=0, finished=False, error=None)
    yield


def _make_client(tmp_path, monkeypatch, *, seed=None, override=None):
    db = tmp_path / "tr.db"
    init_db(db)
    conn = get_conn(db)
    if seed is not None:
        seed(conn)
    conn.close()
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    if override is not None:
        app.dependency_overrides[_ai_client] = override
    return TestClient(app)


def _zh_map(db):
    conn = get_conn(db)
    try:
        return {r["full_name"]: r["description_zh"] for r in conn.execute("SELECT full_name, description_zh FROM repos")}
    finally:
        conn.close()


def _wait_batch_finished(client, *, timeout=5.0):
    """真等待轮询 /status 直到 finished（每次 0.15s，总超时 5s 防挂死）；返回最终 state dict。

    后台 worker 跑在 TestClient portal 事件循环（跨请求存活），主线程 sleep 即让出循环推进 worker。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/api/translate-missing/status").json()
        if state["finished"]:
            return state
        time.sleep(0.15)
    raise AssertionError(f"批量任务 {timeout}s 内未完成，最后状态: {state}")


# ---------- POST /api/translate：单个强制重译 ----------


def test_api_translate_overwrites_existing_zh(tmp_path, monkeypatch):
    """强制覆盖：库内已有译文也被新译文覆盖（§7.4 单个重译＝强制）；响应形态完整；不触碰 recommendations。"""

    def seed(conn):
        _add_repo(conn, "a/one", description_en="english text", description_zh="旧译")

    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=seed, override=lambda: fake) as client:
        resp = client.post("/api/translate", json={"full_name": "a/one"})
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"full_name": "a/one", "translated": True, "description_zh": "译文-english text"}
    assert fake.calls == ["english text"]
    assert _zh_map(tmp_path / "tr.db") == {"a/one": "译文-english text"}  # 覆盖写库
    conn = get_conn(tmp_path / "tr.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 0  # 不触碰推荐理由表
    finally:
        conn.close()


def test_api_translate_no_description_400(tmp_path, monkeypatch):
    """无英文简介 → 400 detail"无简介可译"（§7.3 toast 文案）；译文不写。"""

    def seed(conn):
        _add_repo(conn, "a/nodesc", description_en=None)

    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=seed, override=lambda: fake) as client:
        resp = client.post("/api/translate", json={"full_name": "a/nodesc"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "无简介可译"
    assert fake.calls == []
    assert _zh_map(tmp_path / "tr.db") == {"a/nodesc": None}


def test_api_translate_cjk_400(tmp_path, monkeypatch):
    """原文含中文 → 400 detail"原文已是中文，无需翻译"（§7.3）；不送译。"""

    def seed(conn):
        _add_repo(conn, "a/cjk", description_en="web 框架 with 中文混排")

    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=seed, override=lambda: fake) as client:
        resp = client.post("/api/translate", json={"full_name": "a/cjk"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "原文已是中文，无需翻译"
    assert fake.calls == []


def test_api_translate_repo_missing_404(tmp_path, monkeypatch):
    """仓库不在跟踪池 → 404。"""

    with _make_client(tmp_path, monkeypatch, override=lambda: FakeAiClient()) as client:
        resp = client.post("/api/translate", json={"full_name": "ghost/none"})
    assert resp.status_code == 404
    assert "ghost/none" in resp.json()["detail"]


def test_api_translate_failure_keeps_old_zh(tmp_path, monkeypatch):
    """AI 失败 → 502 且旧译文保留（§7.3：失败分支旧译文保留不变，按钮恢复）。"""

    def seed(conn):
        _add_repo(conn, "a/one", description_en="english text", description_zh="旧译")

    fake = FakeAiClient(fail_for={"english text"})
    with _make_client(tmp_path, monkeypatch, seed=seed, override=lambda: fake) as client:
        resp = client.post("/api/translate", json={"full_name": "a/one"})
    assert resp.status_code == 502
    assert _zh_map(tmp_path / "tr.db") == {"a/one": "旧译"}  # 旧译文保留不变


def test_api_translate_bad_body_400(tmp_path, monkeypatch):
    """请求体非 JSON / 缺 full_name → 400（fail-loud，同 /api/follows 口径）。"""
    with _make_client(tmp_path, monkeypatch, override=lambda: FakeAiClient()) as client:
        assert client.post("/api/translate", content="not json").status_code == 400
        assert client.post("/api/translate", json={}).status_code == 400
        assert client.post("/api/translate", json={"full_name": "bad-name"}).status_code == 400  # 形态非法


def test_api_translate_no_key_500(tmp_path, monkeypatch):
    """key 未配置：真 client 使用点拦截 → 500（detail 带修复指引；与 T-011 降级链同姿态）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")

    def seed(conn):
        _add_repo(conn, "a/one", description_en="english text")

    with _make_client(tmp_path, monkeypatch, seed=seed) as client:  # 不 override：走真 DeepSeekClient
        resp = client.post("/api/translate", json={"full_name": "a/one"})
    assert resp.status_code == 500
    assert "DEEPSEEK_API_KEY" in resp.json()["detail"]
    assert _zh_map(tmp_path / "tr.db") == {"a/one": None}  # 不写库


# ---------- POST /api/translate-missing：批量后台任务只补 NULL ＋ running 409 ----------


def test_api_translate_missing_only_fills_null(tmp_path, monkeypatch):
    """批量只补 NULL（后台任务形态）：202 立即返回＋/status 轮询到 finished；已译不覆盖、原文中文/无简介跳过；
    只译真正缺失的；不触碰 recommendations。"""

    def seed(conn):
        _add_repo(conn, "a/done", description_en="english A", description_zh="已译")
        _add_repo(conn, "a/todo", description_en="english B")
        _add_repo(conn, "a/cjk", description_en="中文简介")
        _add_repo(conn, "a/nodesc", description_en=None)

    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        monkeypatch.setattr(routes, "_make_ai_client", lambda: fake)  # worker 自造 client 走模块工厂（后台任务无请求依赖）
        resp = client.post("/api/translate-missing")
        assert resp.status_code == 202  # 后台形态：立即返回，不在请求内跑完
        assert resp.json() == {"started": True, "total": 1}  # 已译/中文/无简介不计入 total
        state = _wait_batch_finished(client)
        assert state["running"] is False
        assert state["finished"] is True
        assert state["error"] is None
        assert state["translated"] == 1 and state["failed"] == 0
    assert fake.calls == ["english B"]  # 只送译真正缺失且可译的一条
    zh = _zh_map(tmp_path / "tr.db")
    assert zh["a/todo"] == "译文-english B"
    assert zh["a/done"] == "已译"  # 不覆盖已有译文
    assert zh["a/cjk"] is None and zh["a/nodesc"] is None
    conn = get_conn(tmp_path / "tr.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 0  # 不触碰推荐理由表
    finally:
        conn.close()


def test_api_translate_missing_partial_failure_degrades(tmp_path, monkeypatch):
    """批量中途单条失败（后台任务）：不炸、失败计入 failed 整批继续、库内不留半成品、其余正常（降级链同 ensure）。"""

    def seed(conn):
        _add_repo(conn, "a/bad", description_en="boom text")
        _add_repo(conn, "a/good", description_en="good text")

    fake = FakeAiClient(fail_for={"boom text"})
    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        monkeypatch.setattr(routes, "_make_ai_client", lambda: fake)
        resp = client.post("/api/translate-missing")
        assert resp.status_code == 202
        assert resp.json() == {"started": True, "total": 2}
        state = _wait_batch_finished(client)
        assert state["finished"] is True and state["error"] is None
        assert state["translated"] == 1 and state["failed"] == 1
    zh = _zh_map(tmp_path / "tr.db")
    assert zh["a/bad"] is None  # 失败不落库：次日/再触发自然补缺
    assert zh["a/good"] == "译文-good text"


def test_api_translate_missing_inflight_409(tmp_path, monkeypatch):
    """running 态 409：后台 worker 挂起窗口内重复 POST → 409（§7.3 toast"补译进行中…"）；任务完成后状态收敛。"""

    def seed(conn):
        _add_repo(conn, "a/one", description_en="english text")

    entered = threading.Event()
    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        monkeypatch.setattr(routes, "_make_ai_client", lambda: BlockingAiClient(entered))
        resp1 = client.post("/api/translate-missing")  # 202 立即返回，worker 在后台挂起
        assert resp1.status_code == 202
        assert entered.wait(timeout=5)  # worker 已进入翻译中（in-flight 窗口）
        resp2 = client.post("/api/translate-missing")
        assert resp2.status_code == 409  # 进行中重复触发（§7.3）
        state = _wait_batch_finished(client)
        assert state["finished"] is True and state["error"] is None
        assert state["translated"] == 1 and state["failed"] == 0
    assert _zh_map(tmp_path / "tr.db") == {"a/one": "译文-english text"}


def test_api_translate_missing_no_key_auth_error(tmp_path, monkeypatch):
    """key 未配置：worker 内真 client 使用点拦截（不发请求）→ finished 且 error=="auth"（前端 toast
    "未配置 DeepSeek API key"）；不写库。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")

    def seed(conn):
        _add_repo(conn, "a/one", description_en="english text")

    with _make_client(tmp_path, monkeypatch, seed=seed) as client:  # 不 monkeypatch 工厂：真 client 空 key 拦截
        resp = client.post("/api/translate-missing")
        assert resp.status_code == 202
        state = _wait_batch_finished(client)
        assert state["finished"] is True and state["error"] == "auth"
        assert state["translated"] == 0 and state["failed"] == 0
    assert _zh_map(tmp_path / "tr.db") == {"a/one": None}  # 未写任何译文


def test_api_translate_missing_worker_conn_crash_unknown(tmp_path, monkeypatch):
    """worker 启动即炸（get_conn 抛异常，F2-1 修复锁定）：连接获取移入 try 后异常落 error="unknown"，
    终态收敛（running=False/finished=True，不卡死"补译中…"），任务无未捕获异常。"""

    def seed(conn):
        _add_repo(conn, "a/one", description_en="english text")

    real_get_conn = routes.get_conn
    call_count = {"n": 0}

    def flaky_conn(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] > 1:  # 第 1 次是 POST 侧（请求内），第 2 次是 worker（后台任务内）——worker 这次炸
            raise RuntimeError("模拟 worker get_conn 崩溃")
        return real_get_conn(*args, **kwargs)

    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        monkeypatch.setattr(routes, "get_conn", flaky_conn)
        resp = client.post("/api/translate-missing")
        assert resp.status_code == 202  # 任务已起，202 立即返回
        state = _wait_batch_finished(client)
        assert state["finished"] is True and state["running"] is False  # 状态不卡死（F2-1）
        assert state["error"] == "unknown"
        assert state["translated"] == 0 and state["failed"] == 0
    task = routes._batch_task
    assert task.done() and task.exception() is None  # 无未捕获异常冒到事件循环


def test_api_translate_missing_second_post_resets_counters(tmp_path, monkeypatch):
    """任务完成后再次 POST：202 且计数重置为新一轮（§7.2 下次 POST 重置）——新一轮 status 的
    total/translated 不含上一轮数字。"""

    def seed(conn):
        _add_repo(conn, "a/one", description_en="english text")
        _add_repo(conn, "a/two", description_en="english two")

    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        monkeypatch.setattr(routes, "_make_ai_client", lambda: fake)
        resp1 = client.post("/api/translate-missing")
        assert resp1.status_code == 202
        assert resp1.json() == {"started": True, "total": 2}
        state1 = _wait_batch_finished(client)
        assert state1["translated"] == 2 and state1["failed"] == 0
        # 第二轮：全部已译 → 新一轮 total=0（若未重置会残留上一轮数字）
        resp2 = client.post("/api/translate-missing")
        assert resp2.status_code == 202
        assert resp2.json() == {"started": True, "total": 0}
        state2 = _wait_batch_finished(client)
        assert state2["total"] == 0 and state2["translated"] == 0 and state2["failed"] == 0
        assert state2["error"] is None and state2["finished"] is True and state2["running"] is False
    assert fake.calls == ["english text", "english two"]  # 第二轮无待译，不再送译


def test_translate_missing_status_default(tmp_path, monkeypatch):
    """无任务史：GET /status 返回全零/false/None 默认态（不依赖 AI client）。"""
    with _make_client(tmp_path, monkeypatch) as client:
        assert client.get("/api/translate-missing/status").json() == {
            "running": False,
            "translated": 0,
            "failed": 0,
            "total": 0,
            "finished": False,
            "error": None,
        }


# ---------- SSR 形态：行内按钮按态文案 ＋ 顶栏批量按钮 ----------


def test_row_translate_button_ssr_states(tmp_path, monkeypatch):
    """行内按钮按态（§7.1）：无译文"翻译"＋data-has-zh=0；有译文"重新翻译"＋data-has-zh=1；顶栏批量按钮就位。"""

    def seed(conn):
        _add_repo(conn, "a/en", description_en="english text")
        _add_repo(conn, "a/zh", description_en="english too", description_zh="已译")

    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        text = client.get("/total?board=all").text  # T-026：board=all 全量语境（a/en/a/zh 无语言 → 其它语言榜）
    assert 'class="translate-btn" data-repo="a/en" data-has-zh="0">翻译</button>' in text
    assert 'class="translate-btn" data-repo="a/zh" data-has-zh="1">重新翻译</button>' in text
    header_section = text.split("</header>", 1)[0]  # 顶栏段：批量按钮必须在 header.top 内
    footer_section = text.split("<footer", 1)[1].split("</footer>", 1)[0]  # footer 元素本体（meta 行说明文字）
    assert '<button class="translate-all" id="translate-all">补译全部缺失</button>' in header_section  # 顶栏批量按钮
    assert "translate-all" not in footer_section  # 页脚不再有批量按钮（§7.1 页脚→顶栏）


# ---------- 低-6 收口（T-017 顺带）：批量 worker 写库护栏（并发现值竞态防护） ----------


def test_api_translate_missing_scope_excludes_outside_repos(tmp_path, monkeypatch):
    """F1-1 修复（T-017 决策 5 v3 收窄）：批量补译只送译范围集 S（三口径榜 ∪ 关注集）内未译仓；
    范围外（未上榜未关注，如 dead）不送译、不计入 total。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def seed(conn):
        _add_repo(conn, "a/onboard", description_en="english text")
        dead_id = _add_repo(conn, "a/dead", description_en="dead english")
        conn.execute("UPDATE repos SET dead = 1 WHERE id = ?", (dead_id,))
        conn.commit()

    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        monkeypatch.setattr(routes, "_make_ai_client", lambda: fake)
        resp = client.post("/api/translate-missing")
        assert resp.status_code == 202
        assert resp.json() == {"started": True, "total": 1}  # 只计范围内未译（a/dead 不在 S）
        state = _wait_batch_finished(client)
        assert state["translated"] == 1 and state["failed"] == 0
    assert fake.calls == ["english text"]  # 只送译范围内一条
    zh = _zh_map(tmp_path / "tr.db")
    assert zh["a/onboard"] == "译文-english text"
    assert zh["a/dead"] is None  # 范围外不译


def test_api_translate_missing_guard_skips_concurrent_write(tmp_path, monkeypatch):
    """worker UPDATE 带护栏（id + description_zh IS NULL + description_en 原值）：worker 拎起本条后、
    UPDATE 落库前被并发写入的仓库 → rowcount=0 跳过不重复计数、不覆盖并发写入值（写库前不重查现值的竞态防护）。

    确定性编排水闸：fake.translate 进入即 set entered、阻塞等 release——保证并发写入严格落在
    worker 的 SELECT 之后、UPDATE 之前（直接 POST 后裸写库是赛跑，worker 快一步就误绿/误红）。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def seed(conn):
        _add_repo(conn, "a/one", description_en="english text")

    entered = threading.Event()  # worker 已进入 translate（SELECT 已过、UPDATE 未到）
    release = threading.Event()  # 测试线程并发写入完成后放行 worker

    class BlockingFakeAiClient(FakeAiClient):
        async def translate(self, text: str) -> str:
            entered.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait)
            return await super().translate(text)

    fake = BlockingFakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        monkeypatch.setattr(routes, "_make_ai_client", lambda: fake)
        resp = client.post("/api/translate-missing")
        assert resp.status_code == 202
        assert entered.wait(timeout=5), "worker 未在 5s 内拎起待译条目"
        # worker 已被水闸拦在 translate 内（SELECT 之后）：此刻并发写入（模拟单个翻译 API 抢先落库）
        conn = get_conn(tmp_path / "tr.db")
        try:
            conn.execute("UPDATE repos SET description_zh = '并发写入' WHERE full_name = 'a/one'")
            conn.commit()
        finally:
            conn.close()
        release.set()  # 放行 worker：其 UPDATE 护栏应命中跳过本条
        state = _wait_batch_finished(client)
        assert state["finished"] is True and state["error"] is None
        assert state["translated"] == 0 and state["failed"] == 0  # 护栏命中：本条不计成功不计失败
    assert _zh_map(tmp_path / "tr.db") == {"a/one": "并发写入"}  # 不覆盖并发写入值
