"""T-016 翻译 API 测试：TestClient 打真实 app（lifespan 会跑 init_db），tmp_path 独立库 + 调度关闭。

口径钉死（《交互流程说明》§7.3/§7.4）：单个 /api/translate 强制重译（覆盖现值）；
批量 /api/translate-missing 只补 NULL＋in-flight 锁（重复触发 409）；两者不触碰 recommendations 表。
AI 调用全 mock（dependency_overrides 注入假 client，同 tests/test_follows.py 手法）；key 未配置用例
不 override、走真 DeepSeekClient 使用点拦截（空 key 不发请求直接 DeepSeekAuthError）。
"""

import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

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


# ---------- POST /api/translate-missing：批量只补 NULL ＋ in-flight 锁 ----------


def test_api_translate_missing_only_fills_null(tmp_path, monkeypatch):
    """批量只补 NULL：已译不覆盖、原文中文/无简介跳过；只译真正缺失的；不触碰 recommendations。"""

    def seed(conn):
        _add_repo(conn, "a/done", description_en="english A", description_zh="已译")
        _add_repo(conn, "a/todo", description_en="english B")
        _add_repo(conn, "a/cjk", description_en="中文简介")
        _add_repo(conn, "a/nodesc", description_en=None)

    fake = FakeAiClient()
    with _make_client(tmp_path, monkeypatch, seed=seed, override=lambda: fake) as client:
        resp = client.post("/api/translate-missing")
    assert resp.status_code == 200
    assert resp.json() == {"translated": 1, "failed": 0}
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
    """批量中途单条失败：不炸（200）、失败条不计入 translated、库内不留半成品、其余正常（降级链同 ensure）。"""

    def seed(conn):
        _add_repo(conn, "a/bad", description_en="boom text")
        _add_repo(conn, "a/good", description_en="good text")

    fake = FakeAiClient(fail_for={"boom text"})
    with _make_client(tmp_path, monkeypatch, seed=seed, override=lambda: fake) as client:
        resp = client.post("/api/translate-missing")
    assert resp.status_code == 200
    assert resp.json() == {"translated": 1, "failed": 1}
    zh = _zh_map(tmp_path / "tr.db")
    assert zh["a/bad"] is None  # 失败不落库：次日/再触发自然补缺
    assert zh["a/good"] == "译文-good text"


def test_api_translate_missing_inflight_409(tmp_path, monkeypatch):
    """in-flight 锁：批量进行中重复触发 → 409（§7.3 toast"补译进行中…"）；首个请求完成后锁释放。"""

    def seed(conn):
        _add_repo(conn, "a/one", description_en="english text")

    entered = threading.Event()
    with _make_client(tmp_path, monkeypatch, seed=seed, override=lambda: BlockingAiClient(entered)) as client:
        results: dict = {}

        def worker():
            results["first"] = client.post("/api/translate-missing")  # 另一线程发首个请求（translate 内挂起 0.5s）

        t = threading.Thread(target=worker)
        t.start()
        assert entered.wait(timeout=5)  # 首个请求已进入翻译中（in-flight 窗口）
        resp2 = client.post("/api/translate-missing")
        assert resp2.status_code == 409  # 进行中重复触发（§7.3）
        t.join(timeout=5)
        assert not t.is_alive()
        assert results["first"].status_code == 200  # 首请求正常完成
        assert _zh_map(tmp_path / "tr.db") == {"a/one": "译文-english text"}


def test_api_translate_missing_no_key_500(tmp_path, monkeypatch):
    """key 未配置：批量整轮失败 → 500（detail 带修复指引；前端 toast"未配置 DeepSeek API key"）。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")

    def seed(conn):
        _add_repo(conn, "a/one", description_en="english text")

    with _make_client(tmp_path, monkeypatch, seed=seed) as client:  # 不 override：真 client 使用点拦截
        resp = client.post("/api/translate-missing")
    assert resp.status_code == 500
    assert "DEEPSEEK_API_KEY" in resp.json()["detail"]
    assert _zh_map(tmp_path / "tr.db") == {"a/one": None}  # 未写任何译文


# ---------- SSR 形态：行内按钮按态文案 ＋ 页脚批量按钮 ----------


def test_row_translate_button_ssr_states(tmp_path, monkeypatch):
    """行内按钮按态（§7.1）：无译文"翻译"＋data-has-zh=0；有译文"重新翻译"＋data-has-zh=1；页脚批量按钮就位。"""

    def seed(conn):
        _add_repo(conn, "a/en", description_en="english text")
        _add_repo(conn, "a/zh", description_en="english too", description_zh="已译")

    with _make_client(tmp_path, monkeypatch, seed=seed) as client:
        text = client.get("/total").text
    assert 'class="translate-btn" data-repo="a/en" data-has-zh="0">翻译</button>' in text
    assert 'class="translate-btn" data-repo="a/zh" data-has-zh="1">重新翻译</button>' in text
    assert '<button class="translate-all" id="translate-all">补译全部缺失</button>' in text  # 页脚批量按钮
