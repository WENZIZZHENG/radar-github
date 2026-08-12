"""每日任务与调度注册单测：假 client + tmp_path 真实 sqlite 文件，全程离线，禁真打 API。

不引 pytest-asyncio（不加新依赖）：异步用例统一 asyncio.run 驱动，与 test_github.py / test_snapshot.py 同口径。
"""

import asyncio
import logging

from fastapi.testclient import TestClient

from app.collector.discover import DailyStats, run_daily
from app.collector.github import GitHubAuthError
from app.db import get_conn, init_db
from app.jobs import DAILY_JOB_ID, create_scheduler, daily_job
from app.main import app

NOW = "2026-08-09T00:00:00Z"


def make_node(
    full_name: str,
    *,
    stars: int = 1500,
    private: bool = False,
    disabled: bool = False,
    description: str | None = None,
    language: str | None = None,
    topics: tuple[str, ...] = (),
) -> dict:
    """造一条 GraphQL Repository 节点（字段对齐每日任务用到的子集）。

    description/language 默认 None：既有用例库里对应字段也是 NULL，两值相等 → 不触发变更检测；
    topics 默认空：与 schema 默认 '[]' 相等（T-025 起比对 language/topics，默认值即"无漂移"，不误伤既有用例）。
    """
    return {
        "nameWithOwner": full_name,
        "stargazerCount": stars,
        "isPrivate": private,
        "isDisabled": disabled,
        "description": description,
        "primaryLanguage": {"name": language} if language is not None else None,
        "repositoryTopics": {"nodes": [{"topic": {"name": t}} for t in topics]},
    }


def make_search_item(full_name: str, node_id: str, *, stars: int = 1500, description="d", language="Go", topics=("ai",)) -> dict:
    """造一条 REST search item（发现池入池要用的字段子集）。"""
    return {
        "full_name": full_name,
        "node_id": node_id,
        "description": description,
        "language": language,
        "topics": list(topics),
        "stargazers_count": stars,
    }


class FakeClient:
    """假 GitHub client：nodes 按 node_id→节点脚本返回（缺省 None＝死库信号）；search 按页脚本返回。

    search_pages 按页号返回（每日发现查询用）；search_pages_by_query 按 query 精确区分返回
    （T-019 补捞查询与每日查询需返回不同内容时用，缺省回落 search_pages，不影响既有用例）。
    fail_batches / fail_pages 模拟单批/单页失败；raise_always 模拟整轮异常。记录所有调用供断言。
    """

    def __init__(
        self,
        *,
        nodes_by_id: dict | None = None,
        search_pages: dict | None = None,
        search_pages_by_query: dict | None = None,
        fail_batches: set = (),
        fail_pages: set = (),
        fail_queries: set = (),
        raise_always: bool = False,
    ) -> None:
        self.nodes_by_id = nodes_by_id or {}
        self.search_pages = search_pages or {}
        self.search_pages_by_query = search_pages_by_query or {}
        self.fail_batches = set(fail_batches)
        self.fail_pages = set(fail_pages)
        self.fail_queries = set(fail_queries)
        self.raise_always = raise_always
        self.fetch_calls: list[list[str]] = []
        self.search_calls: list[tuple[str, str, int]] = []

    async def fetch_repos_by_ids(self, ids: list[str]) -> list[dict | None]:
        batch_index = len(self.fetch_calls)
        self.fetch_calls.append(list(ids))
        if self.raise_always or batch_index in self.fail_batches:
            raise RuntimeError("模拟批次失败")
        return [self.nodes_by_id.get(node_id) for node_id in ids]

    async def search_repositories(self, query: str, *, sort: str = "stars", per_page: int = 100, page: int = 1) -> dict:
        self.search_calls.append((query, sort, page))
        # fail_queries 非空时失败限定到指定 query（每日/补捞查询各自翻页互不影响），空则按页号全局
        if self.raise_always or (page in self.fail_pages and (not self.fail_queries or query in self.fail_queries)):
            raise RuntimeError("模拟翻页失败")
        pages = self.search_pages_by_query.get(query, self.search_pages)
        return {"total_count": 0, "items": pages.get(page, [])}


def _open_db(tmp_path):
    db_path = tmp_path / "radar.db"
    init_db(db_path)
    return get_conn(db_path)


def seed_repo(conn, full_name: str, node_id: str, *, dead: int = 0, source: str = "initial") -> int:
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, dead, source, created_at) VALUES (?, ?, ?, ?, ?)",
        (full_name, node_id, dead, source, "2026-08-08T00:00:00Z"),
    )
    conn.commit()
    return cur.lastrowid


def make_logger():
    """捕获日志记录的测试 logger：每用例重建 handler，互不串记录。"""
    logger = logging.getLogger("test-jobs")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    records: list[logging.LogRecord] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger.addHandler(ListHandler())
    return logger, records


def _run(client, conn, logger, *, now=NOW):
    return asyncio.run(run_daily(client, conn, now_iso=lambda: now, log=logger))


# ---------- 每日快照：nodes → snapshots 行，死信号 → dead=1 ----------


def test_daily_snapshot_maps_nodes_and_marks_dead(tmp_path):
    conn = _open_db(tmp_path)
    id_live = seed_repo(conn, "a/live", "nid-live")
    seed_repo(conn, "a/gone", "nid-gone")
    seed_repo(conn, "a/private", "nid-private")
    seed_repo(conn, "a/disabled", "nid-disabled")
    seed_repo(conn, "a/dead-already", "nid-dead", dead=1)
    client = FakeClient(
        nodes_by_id={
            "nid-live": make_node("a/live", stars=4321),
            "nid-gone": None,  # 删除/转私：nodes 返回 null
            "nid-private": make_node("a/private", private=True),
            "nid-disabled": make_node("a/disabled", disabled=True),
        }
    )
    logger, _records = make_logger()
    stats = _run(client, conn, logger)

    assert stats.snapshots_written == 1
    assert stats.dead_marked == 3
    snaps = conn.execute("SELECT repo_id, captured_at, stars FROM star_snapshots").fetchall()
    assert [(s["repo_id"], s["captured_at"], s["stars"]) for s in snaps] == [(id_live, NOW, 4321)]
    dead_by_name = {r["full_name"]: r["dead"] for r in conn.execute("SELECT full_name, dead FROM repos")}
    assert dead_by_name == {"a/live": 0, "a/gone": 1, "a/private": 1, "a/disabled": 1, "a/dead-already": 1}
    # 已死仓库不再采集：node_id 不出现在任何 nodes 请求里
    assert all("nid-dead" not in ids for ids in client.fetch_calls)
    conn.close()


# ---------- 发现池：新面孔入池（含 node_id）＋基线快照；已存在不动 ----------


def test_discover_inserts_new_faces_with_node_id_and_baseline(tmp_path):
    conn = _open_db(tmp_path)
    seed_repo(conn, "a/live", "nid-live")
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("a/live", stars=2000)},
        search_pages={
            1: [make_search_item("new/one", "nid-new-1", stars=1500)],
            # 页间重复（updated 排序抖动）：只入池一次
            2: [
                make_search_item("new/one", "nid-new-1", stars=1500),
                make_search_item("new/two", "nid-new-2", stars=1200, description=None, language=None, topics=()),
            ],
        },
    )
    logger, _records = make_logger()
    stats = _run(client, conn, logger)

    assert stats.discovered == 2
    rows = conn.execute(
        "SELECT full_name, node_id, description_en, language, topics, dead FROM repos WHERE source = 'discover'"
    ).fetchall()
    assert {r["full_name"] for r in rows} == {"new/one", "new/two"}
    one = next(r for r in rows if r["full_name"] == "new/one")
    assert one["node_id"] == "nid-new-1"  # 入池必须带 node_id：次日 nodes(ids:) 采集的入口
    assert one["description_en"] == "d" and one["language"] == "Go" and one["topics"] == '["ai"]'
    assert one["dead"] == 0
    two = next(r for r in rows if r["full_name"] == "new/two")
    assert two["description_en"] is None and two["language"] is None and two["topics"] == "[]"
    # 基线快照当行写入，captured_at 与整轮共享（同批一致硬约定）
    snaps = conn.execute(
        "SELECT r.full_name, s.captured_at, s.stars FROM star_snapshots s JOIN repos r ON r.id = s.repo_id "
        "WHERE r.source = 'discover'"
    ).fetchall()
    assert {(s["full_name"], s["captured_at"], s["stars"]) for s in snaps} == {
        ("new/one", NOW, 1500),
        ("new/two", NOW, 1200),
    }
    # 发现池确实按 updated 排序捞"最近活跃"新面孔，而非默认星数榜
    assert {call[0] for call in client.search_calls} == {"stars:>=1000"}
    assert all(call[1] == "updated" for call in client.search_calls)
    conn.close()


def test_discover_leaves_existing_repos_untouched(tmp_path):
    """已存在的仓库（含死库）发现池一律不动：不重复入池、不改字段、不复活、不多写快照。"""
    conn = _open_db(tmp_path)
    id_live = seed_repo(conn, "a/live", "nid-live")
    seed_repo(conn, "a/dead", "nid-dead", dead=1)
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("a/live", stars=2000)},
        search_pages={
            1: [
                make_search_item("a/live", "nid-live", stars=9999),
                make_search_item("a/dead", "nid-dead", stars=3000),
            ]
        },
    )
    logger, _records = make_logger()
    stats = _run(client, conn, logger)

    assert stats.discovered == 0
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 2
    row = conn.execute("SELECT dead, source FROM repos WHERE full_name = 'a/dead'").fetchone()
    assert row["dead"] == 1 and row["source"] == "initial"  # 死库不因发现池复活
    # a/live 只有快照阶段一行；发现池不重复写基线
    assert conn.execute("SELECT COUNT(*) FROM star_snapshots WHERE repo_id = ?", (id_live,)).fetchone()[0] == 1
    conn.close()


# ---------- 静默容错：单批失败后续继续；整任务异常吞掉记日志 ----------


def test_batch_failure_is_logged_and_following_batches_continue(tmp_path):
    conn = _open_db(tmp_path)
    ids = [seed_repo(conn, f"o/r{i:03d}", f"nid-{i:03d}") for i in range(101)]  # 101 个 → 2 批（100+1）
    client = FakeClient(nodes_by_id={"nid-100": make_node("o/r100", stars=777)}, fail_batches={0})
    logger, records = make_logger()
    stats = _run(client, conn, logger)

    assert len(client.fetch_calls) == 2  # 失败不中断：两批都尝试了
    assert stats.snapshots_written == 1  # 仅第 2 批成功
    assert conn.execute("SELECT stars FROM star_snapshots WHERE repo_id = ?", (ids[100],)).fetchone()["stars"] == 777
    assert any("快照批次失败" in r.getMessage() for r in records)
    assert any("每日任务汇总" in r.getMessage() for r in records)  # 每轮必有汇总行
    conn.close()


def test_page_failure_is_logged_and_following_pages_continue(tmp_path):
    conn = _open_db(tmp_path)
    client = FakeClient(
        search_pages={
            2: [make_search_item("new/p2", "nid-p2", stars=1100)],
        },
        fail_pages={1},
    )
    logger, records = make_logger()
    stats = _run(client, conn, logger)

    assert stats.discovered == 1  # 第 1 页挂了，第 2 页的新面孔仍入池
    assert conn.execute("SELECT full_name FROM repos WHERE source = 'discover'").fetchone()["full_name"] == "new/p2"
    assert any("发现池第 1 页拉取失败" in r.getMessage() for r in records)
    conn.close()


def test_whole_task_exception_swallowed_and_logged(tmp_path):
    """整任务异常（如库不可用）：不抛出、有日志、返回零统计（共识 §8 次日自然重试）。"""
    conn = _open_db(tmp_path)
    conn.close()  # 关库制造整轮异常：首个 SELECT 即炸
    logger, records = make_logger()
    stats = _run(FakeClient(), conn, logger)
    assert stats == DailyStats()
    assert any("每日任务整轮异常" in r.getMessage() for r in records)
    assert any("每日任务汇总" in r.getMessage() for r in records)


# ---------- 调度注册：UTC 00:00 cron＋错过策略；main.py 开关接线 ----------


def test_create_scheduler_registers_daily_utc_cron():
    scheduler = create_scheduler()  # 未启动：get_jobs 返回 pending job，注册参数离线可断言
    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == DAILY_JOB_ID
    assert job.func is daily_job  # 注册的确实是每日任务本体，不是别的函数
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "0" and fields["minute"] == "0"  # 每日 UTC 00:00
    assert "UTC" in str(job.trigger.timezone)  # 时区钉死 UTC，不随服务器本地时区漂移
    assert job.misfire_grace_time == 3600
    assert job.coalesce is True


def test_main_startup_skips_scheduler_when_jobs_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "radar.db"))  # lifespan 的 init_db 落 tmp，不碰真实库
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    created = []
    monkeypatch.setattr("app.main.create_scheduler", lambda: created.append(True))
    with TestClient(app) as client:  # 上下文管理器形式才跑 lifespan
        assert client.get("/").status_code == 200
    assert created == []  # 调度器压根没建


def test_main_startup_starts_and_stops_scheduler_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "radar.db"))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "1")
    events = []

    class SpyScheduler:
        pass

    monkeypatch.setattr("app.main.create_scheduler", lambda: SpyScheduler())
    monkeypatch.setattr("app.main.start_scheduler", lambda s: events.append("start"))
    monkeypatch.setattr("app.main.stop_scheduler", lambda s: events.append("stop"))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert events == ["start"]  # startup 拉起
    assert events == ["start", "stop"]  # shutdown 优雅停止



# ---------- 评审 findings 修复锁定 ----------


def test_renamed_repo_node_id_conflict_skipped(tmp_path):
    """改名仓库（full_name 变、node_id 已有）：双键判重按 node_id 拦截跳过——不触发反查落空 KeyError 整批回滚。"""
    conn = _open_db(tmp_path)
    seed_repo(conn, "old/name", "nid-same")  # 改名前已在池
    client = FakeClient(
        nodes_by_id={"nid-same": make_node("old/name", stars=2000)},
        search_pages={1: [make_search_item("brand/new-name", "nid-same", stars=3000)]},  # 改名后出现在发现池
    )
    logger, _records = make_logger()
    stats = _run(client, conn, logger)
    assert stats.discovered == 0  # 改名 ≠ 新面孔
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1
    assert conn.execute("SELECT full_name FROM repos").fetchone()["full_name"] == "old/name"  # 原名不动
    conn.close()


def test_auth_error_aborts_run_without_batch_retry(tmp_path):
    """token 失效（确定性错误）：第一批即直通整轮 handler 记一次日志，不逐批重试刷爆日志。"""

    class AuthFailClient(FakeClient):
        async def fetch_repos_by_ids(self, ids):
            self.fetch_calls.append(list(ids))
            raise GitHubAuthError("GitHub token 无效或已过期")

    conn = _open_db(tmp_path)
    for i in range(3):
        seed_repo(conn, f"a/r{i}", f"nid-{i}")
    client = AuthFailClient()
    logger, records = make_logger()
    stats = _run(client, conn, logger)
    assert len(client.fetch_calls) == 1  # 第一批即直通
    assert stats.snapshots_written == 0
    assert any("每日任务整轮异常" in r.getMessage() for r in records)  # 只记一次
    assert not any("快照批次失败" in r.getMessage() for r in records)
    conn.close()


def test_same_captured_at_rerun_idempotent(tmp_path):
    """同日重跑（同 captured_at）：INSERT OR REPLACE 覆盖，快照行数不长（评审低-3②补测）。"""
    conn = _open_db(tmp_path)
    id_live = seed_repo(conn, "a/live", "nid-live")
    client = FakeClient(nodes_by_id={"nid-live": make_node("a/live", stars=2000)})
    logger, _records = make_logger()
    _run(client, conn, logger)
    _run(client, conn, logger)  # now_iso 固定 NOW：同 captured_at 再跑一轮
    assert conn.execute("SELECT COUNT(*) FROM star_snapshots WHERE repo_id = ?", (id_live,)).fetchone()[0] == 1
    conn.close()


# ---------- T-016 变更检测：description_en 比对更新（§7.4 钉死：只覆盖 description_en） ----------


def _seed_repo_with_desc(conn, *, description_en, description_zh=None):
    """插一个带简介/译文（可空）的仓库，返回 repo_id。"""
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at)"
        " VALUES (?, ?, ?, ?, 'Python', '[\"ai\"]', 0, 'initial', ?)",
        ("a/live", "nid-live", description_en, description_zh, "2026-08-08T00:00:00Z"),
    )
    conn.commit()
    return cur.lastrowid


def test_snapshot_description_change_updates_en_and_clears_zh(tmp_path):
    """原文变更 → UPDATE description_en 新值＋description_zh=NULL（当日 ensure 全池翻译自然重译）；language/topics 与 nodes 一致不动。"""
    conn = _open_db(tmp_path)
    _seed_repo_with_desc(conn, description_en="old desc", description_zh="旧译")
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("a/live", stars=2000, description="new desc", language="Python", topics=("ai",))}
    )
    logger, _records = make_logger()
    _run(client, conn, logger)

    row = conn.execute(
        "SELECT description_en, description_zh, language, topics, dead FROM repos WHERE full_name = 'a/live'"
    ).fetchone()
    assert row["description_en"] == "new desc"  # 原文更新为 nodes 现值
    assert row["description_zh"] is None  # 清旧译文：当日 ensure 重译
    assert row["language"] == "Python" and row["topics"] == '["ai"]'  # language/topics 无漂移：不动（§7.4 T-025）
    assert row["dead"] == 0
    # 快照照常写入（变更检测是附带更新，不改变快照行为）
    repo_id = conn.execute("SELECT id FROM repos WHERE full_name = 'a/live'").fetchone()["id"]
    assert conn.execute("SELECT COUNT(*) FROM star_snapshots WHERE repo_id = ?", (repo_id,)).fetchone()[0] == 1
    conn.close()


def test_snapshot_description_unchanged_keeps_translation(tmp_path):
    """原文无变更 → 不动原文不动译文（§7.3 自动分支口径）。"""
    conn = _open_db(tmp_path)
    _seed_repo_with_desc(conn, description_en="same desc", description_zh="已译")
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("a/live", stars=2000, description="same desc", language="Python", topics=("ai",))}
    )
    logger, _records = make_logger()
    _run(client, conn, logger)

    row = conn.execute("SELECT description_en, description_zh FROM repos WHERE full_name = 'a/live'").fetchone()
    assert row["description_en"] == "same desc" and row["description_zh"] == "已译"
    conn.close()


def test_snapshot_description_removed_clears_en_and_zh(tmp_path):
    """简介被删除（nodes 返回 None）→ 原文清 NULL＋清译文；快照照写（简介删除也是变更）。"""
    conn = _open_db(tmp_path)
    _seed_repo_with_desc(conn, description_en="was here", description_zh="已译")
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("a/live", stars=2000, description=None, language="Python", topics=("ai",))}
    )
    logger, _records = make_logger()
    _run(client, conn, logger)

    row = conn.execute("SELECT description_en, description_zh FROM repos WHERE full_name = 'a/live'").fetchone()
    assert row["description_en"] is None and row["description_zh"] is None
    assert conn.execute("SELECT COUNT(*) FROM star_snapshots").fetchone()[0] == 1
    conn.close()


# ---------- T-017：简介变更连带清全部维度推荐语（与译文清除同一检测点，当日 ensure 范围内重生） ----------


def test_snapshot_description_change_clears_all_recommendations(tmp_path):
    """description_en 变更 → 同事务 DELETE 该仓全部维度推荐语（可再生数据）；译文同样清除。"""
    conn = _open_db(tmp_path)
    repo_id = _seed_repo_with_desc(conn, description_en="old desc", description_zh="旧译")
    conn.execute(
        "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
        " VALUES (?, 'week', '2026-W32', '周文本', NULL, '2026-W32'),"
        " (?, 'total', 'all', '总星文本', 'sha-1', '2026-W32')",
        (repo_id, repo_id),
    )
    conn.commit()
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("a/live", stars=2000, description="new desc", language="Python", topics=("ai",))}
    )
    logger, _records = make_logger()
    _run(client, conn, logger)

    row = conn.execute("SELECT description_en, description_zh FROM repos WHERE full_name = 'a/live'").fetchone()
    assert row["description_en"] == "new desc" and row["description_zh"] is None
    # 全部维度推荐语同事务清除（简介变更 → 当日 ensure 对范围内仓重生自愈）
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE repo_id = ?", (repo_id,)).fetchone()[0] == 0
    conn.close()


def test_snapshot_description_unchanged_keeps_recommendations(tmp_path):
    """简介无变更 → 不动推荐语（README 变更检测是 ensure 的事，采集层不碰）。"""
    conn = _open_db(tmp_path)
    repo_id = _seed_repo_with_desc(conn, description_en="same desc", description_zh="已译")
    conn.execute(
        "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
        " VALUES (?, 'total', 'all', '总星文本', 'sha-1', '2026-W32')",
        (repo_id,),
    )
    conn.commit()
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("a/live", stars=2000, description="same desc", language="Python", topics=("ai",))}
    )
    logger, _records = make_logger()
    _run(client, conn, logger)

    row = conn.execute("SELECT description_zh FROM repos WHERE full_name = 'a/live'").fetchone()
    assert row["description_zh"] == "已译"
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE repo_id = ?", (repo_id,)).fetchone()[0] == 1
    conn.close()


# ---------- T-025：language/topics 漂移检测（§7.4 口径：只 UPDATE 归类字段，不清译文不删推荐语） ----------


def _seed_repo_with_meta(conn, *, language=None, topics='["ai"]', description_en=None, description_zh=None):
    """插一个归类字段/简介/译文全可定制的仓库（T-025 漂移测试用），返回 repo_id。"""
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, 'initial', ?)",
        ("a/live", "nid-live", description_en, description_zh, language, topics, "2026-08-08T00:00:00Z"),
    )
    conn.commit()
    return cur.lastrowid


def test_snapshot_language_drift_updates_language_only(tmp_path):
    """language 漂移 → 只 UPDATE 归类字段：译文保留、推荐语不删（§7.4 T-025 口径）；drift_updated=1。"""
    conn = _open_db(tmp_path)
    repo_id = _seed_repo_with_desc(conn, description_en="same desc", description_zh="已译")  # language='Python', topics='["ai"]'
    conn.execute(
        "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
        " VALUES (?, 'total', 'all', '总星文本', 'sha-1', '2026-W32')",
        (repo_id,),
    )
    conn.commit()
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("a/live", stars=2000, description="same desc", language="Rust", topics=("ai",))}
    )
    logger, _records = make_logger()
    stats = _run(client, conn, logger)

    row = conn.execute("SELECT language, topics, description_zh FROM repos WHERE full_name = 'a/live'").fetchone()
    assert row["language"] == "Rust"  # 归类字段更新
    assert row["topics"] == '["ai"]'  # 未漂移字段写回 nodes 侧新值（同值，字符串不变）
    assert row["description_zh"] == "已译"  # 不清译文
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE repo_id = ?", (repo_id,)).fetchone()[0] == 1  # 不删推荐语
    assert stats.drift_updated == 1
    conn.close()


def test_snapshot_topics_drift_updates_topics_json(tmp_path):
    """topics 增/减 → repos.topics 更新为 nodes 原序 JSON；推荐语不删、译文不动；drift_updated=1。"""
    conn = _open_db(tmp_path)
    repo_id = _seed_repo_with_desc(conn, description_en="same desc", description_zh="已译")
    conn.execute(
        "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
        " VALUES (?, 'total', 'all', '总星文本', 'sha-1', '2026-W32')",
        (repo_id,),
    )
    conn.commit()
    client = FakeClient(
        nodes_by_id={
            "nid-live": make_node("a/live", stars=2000, description="same desc", language="Python", topics=("ai", "ml"))
        }
    )
    logger, _records = make_logger()
    stats = _run(client, conn, logger)

    row = conn.execute("SELECT topics, language, description_zh FROM repos WHERE full_name = 'a/live'").fetchone()
    assert row["topics"] == '["ai", "ml"]'  # 按 GitHub 返回原序 JSON 写回
    assert row["language"] == "Python"
    assert row["description_zh"] == "已译"
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE repo_id = ?", (repo_id,)).fetchone()[0] == 1
    assert stats.drift_updated == 1
    conn.close()


def test_snapshot_topics_order_shuffle_is_not_drift(tmp_path):
    """topics 仅顺序不同（GitHub 返回顺序抖动）→ 按集合比较视为无漂移：不 UPDATE、drift_updated=0。"""
    conn = _open_db(tmp_path)
    _seed_repo_with_meta(conn, language="Python", topics='["ai", "ml"]')  # 库内 [ai, ml]
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("a/live", stars=2000, language="Python", topics=("ml", "ai"))}  # node 反序
    )
    logger, _records = make_logger()
    stats = _run(client, conn, logger)

    row = conn.execute("SELECT topics FROM repos WHERE full_name = 'a/live'").fetchone()
    assert row["topics"] == '["ai", "ml"]'  # 库内字符串原样，不被顺序扰动刷行
    assert stats.drift_updated == 0
    conn.close()


def test_snapshot_no_drift_touches_nothing(tmp_path):
    """language/topics/description 均无变更 → 归类字段/译文/推荐语都不动；drift_updated=0。"""
    conn = _open_db(tmp_path)
    repo_id = _seed_repo_with_desc(conn, description_en="same desc", description_zh="已译")
    conn.execute(
        "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
        " VALUES (?, 'total', 'all', '总星文本', 'sha-1', '2026-W32')",
        (repo_id,),
    )
    conn.commit()
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("a/live", stars=2000, description="same desc", language="Python", topics=("ai",))}
    )
    logger, _records = make_logger()
    stats = _run(client, conn, logger)

    row = conn.execute("SELECT language, topics, description_zh FROM repos WHERE full_name = 'a/live'").fetchone()
    assert (row["language"], row["topics"], row["description_zh"]) == ("Python", '["ai"]', "已译")
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE repo_id = ?", (repo_id,)).fetchone()[0] == 1
    assert stats.drift_updated == 0
    conn.close()


def test_snapshot_language_appears_from_none(tmp_path):
    """language None → 有值（GitHub 补标语言）：库内 NULL 更新为新语言；drift_updated=1。"""
    conn = _open_db(tmp_path)
    _seed_repo_with_meta(conn, language=None, topics='["ai"]')  # 库内 NULL
    client = FakeClient(nodes_by_id={"nid-live": make_node("a/live", stars=2000, language="Go", topics=("ai",))})
    logger, _records = make_logger()
    stats = _run(client, conn, logger)

    row = conn.execute("SELECT language FROM repos WHERE full_name = 'a/live'").fetchone()
    assert row["language"] == "Go"
    assert stats.drift_updated == 1
    conn.close()


def test_snapshot_language_removed_to_none(tmp_path):
    """language 有值 → None（GitHub 官方允许清空语言）：库内更新为 NULL；drift_updated=1。"""
    conn = _open_db(tmp_path)
    _seed_repo_with_meta(conn, language="Go", topics='["ai"]')
    client = FakeClient(nodes_by_id={"nid-live": make_node("a/live", stars=2000, language=None, topics=("ai",))})
    logger, _records = make_logger()
    stats = _run(client, conn, logger)

    row = conn.execute("SELECT language FROM repos WHERE full_name = 'a/live'").fetchone()
    assert row["language"] is None
    assert stats.drift_updated == 1
    conn.close()


def test_snapshot_description_and_drift_both_apply(tmp_path):
    """description 与 language/topics 同仓叠加变更 → 两者都生效：译文清、推荐语删、归类字段更新；drift_updated=1。"""
    conn = _open_db(tmp_path)
    repo_id = _seed_repo_with_desc(conn, description_en="old desc", description_zh="旧译")  # language='Python', topics='["ai"]'
    conn.execute(
        "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)"
        " VALUES (?, 'total', 'all', '总星文本', 'sha-1', '2026-W32')",
        (repo_id,),
    )
    conn.commit()
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("a/live", stars=2000, description="new desc", language="Rust", topics=("ml",))}
    )
    logger, _records = make_logger()
    stats = _run(client, conn, logger)

    row = conn.execute(
        "SELECT description_en, description_zh, language, topics FROM repos WHERE full_name = 'a/live'"
    ).fetchone()
    assert row["description_en"] == "new desc"
    assert row["description_zh"] is None  # description 变更 → 清译文
    assert row["language"] == "Rust" and row["topics"] == '["ml"]'  # 漂移 → 归类字段更新
    assert conn.execute("SELECT COUNT(*) FROM recommendations WHERE repo_id = ?", (repo_id,)).fetchone()[0] == 0  # 删推荐语
    assert stats.drift_updated == 1
    conn.close()


# ---------- T-019：每周补捞（仅 UTC 周一触发，星数区间按 ISO 周序号轮换） ----------

MONDAY_W32 = "2026-08-03T00:00:00Z"  # 周一：ISO W32 → 32%7=4 → bands[4] = 16000..32000（区间语法）
MONDAY_W34 = "2026-08-17T00:00:00Z"  # 周一：ISO W34 → 34%7=6 → bands[6] = 64000..None（开放段语法）


def test_weekly_refill_not_triggered_on_non_monday(tmp_path):
    """非周一（NOW=2026-08-09 周日）：不触发补捞，搜索调用只有每日 updated 查询。"""
    conn = _open_db(tmp_path)
    client = FakeClient()
    logger, _records = make_logger()
    _run(client, conn, logger)

    assert len(client.search_calls) == 5  # 每日 5 页
    assert all(query == "stars:>=1000" and sort == "updated" for query, sort, _page in client.search_calls)
    conn.close()


def test_weekly_refill_triggered_once_with_band_query(tmp_path):
    """周一（W32）：触发且只触发一次补捞，查询为当周波段 stars:16000..32000；新面孔入池＋基线快照。"""
    conn = _open_db(tmp_path)
    band = {"stars:16000..32000": {1: [make_search_item("band/new-a", "nid-band-a", stars=20000)]}}
    client = FakeClient(search_pages_by_query=band)
    logger, records = make_logger()
    stats = _run(client, conn, logger, now=MONDAY_W32)

    # 每日查询 5 次 updated；补捞恰一次（5 页，与每日同配额档）、sort=stars
    daily = [c for c in client.search_calls if c[0] == "stars:>=1000"]
    refills = [c for c in client.search_calls if c[0] != "stars:>=1000"]
    assert len(daily) == 5 and all(c[1] == "updated" for c in daily)
    assert refills == [("stars:16000..32000", "stars", p) for p in range(1, 6)]
    # 新面孔入池＋当行基线快照（captured_at 与整轮共享）
    assert stats.discovered == 1
    rows = conn.execute("SELECT full_name, node_id FROM repos WHERE source = 'discover'").fetchall()
    assert [(r["full_name"], r["node_id"]) for r in rows] == [("band/new-a", "nid-band-a")]
    snaps = conn.execute("SELECT s.captured_at, s.stars FROM star_snapshots s").fetchall()
    assert [(s["captured_at"], s["stars"]) for s in snaps] == [(MONDAY_W32, 20000)]
    # 补捞 INFO 日志：波段、实捞条数、新入池数
    assert any("每周补捞执行" in r.getMessage() and "stars:16000..32000" in r.getMessage() for r in records)
    conn.close()


def test_weekly_refill_band_rotation_open_end(tmp_path):
    """周一（W34）：轮换取模 → 开放段 bands[6]=(64000,None) → stars:>=64000。"""
    conn = _open_db(tmp_path)
    band = {"stars:>=64000": {1: [make_search_item("band/huge", "nid-huge", stars=100000)]}}
    client = FakeClient(search_pages_by_query=band)
    logger, _records = make_logger()
    stats = _run(client, conn, logger, now=MONDAY_W34)

    refills = [c for c in client.search_calls if c[0] != "stars:>=1000"]
    assert refills == [("stars:>=64000", "stars", p) for p in range(1, 6)]
    assert stats.discovered == 1
    assert conn.execute("SELECT full_name FROM repos WHERE source = 'discover'").fetchone()["full_name"] == "band/huge"
    conn.close()


def test_weekly_refill_skips_existing_and_daily_ingested(tmp_path):
    """已在库仓与每日刚入池的仓不重复入池：补捞判重从库重读 seen（同轮每日发现自然覆盖）。"""
    conn = _open_db(tmp_path)
    seed_repo(conn, "old/live", "nid-live")  # 快照阶段会请求 nodes
    daily_pages = {1: [make_search_item("daily/fresh", "nid-daily", stars=1300)]}
    band = {
        "stars:16000..32000": {
            1: [
                make_search_item("old/live", "nid-live", stars=25000),  # 早已在库
                make_search_item("daily/fresh", "nid-daily", stars=25000),  # 每日刚入池
                make_search_item("band/fresh", "nid-band", stars=25000),  # 真新面孔
            ]
        }
    }
    client = FakeClient(
        nodes_by_id={"nid-live": make_node("old/live", stars=2000)},
        search_pages=daily_pages,
        search_pages_by_query=band,
    )
    logger, _records = make_logger()
    stats = _run(client, conn, logger, now=MONDAY_W32)

    assert stats.discovered == 2  # daily/fresh（每日发现）＋ band/fresh（补捞）各计一次
    names = {r["full_name"] for r in conn.execute("SELECT full_name FROM repos WHERE source = 'discover'")}
    assert names == {"daily/fresh", "band/fresh"}
    # 基线快照不重复：每仓恰一行
    snaps = conn.execute(
        "SELECT r.full_name, COUNT(*) FROM star_snapshots s JOIN repos r ON r.id = s.repo_id GROUP BY r.full_name"
    ).fetchall()
    assert {r["full_name"]: r[1] for r in snaps} == {"old/live": 1, "daily/fresh": 1, "band/fresh": 1}
    conn.close()


def test_weekly_refill_page_failure_logged_and_continues(tmp_path):
    """补捞第 2 页失败：记日志继续第 3 页（fail_queries 限定只让补捞查询的页失败，每日查询不受影响）。"""
    conn = _open_db(tmp_path)
    band = {
        "stars:16000..32000": {
            1: [make_search_item("band/p1", "nid-bp1", stars=20000)],
            3: [make_search_item("band/p3", "nid-bp3", stars=20000)],
        }
    }
    client = FakeClient(search_pages_by_query=band, fail_queries={"stars:16000..32000"}, fail_pages={2})
    logger, records = make_logger()
    stats = _run(client, conn, logger, now=MONDAY_W32)

    assert len(client.search_calls) == 10  # 每日 5 页 + 补捞 5 页
    assert stats.discovered == 2
    names = {r["full_name"] for r in conn.execute("SELECT full_name FROM repos WHERE source = 'discover'")}
    assert names == {"band/p1", "band/p3"}
    assert any("每周补捞第 2 页拉取失败" in r.getMessage() for r in records)
    conn.close()


def test_weekly_refill_renamed_repo_not_duplicated(tmp_path):
    """改名仓（node_id 撞已有行）出现在补捞波段：双键判重拦截，不重复入池不炸。"""
    conn = _open_db(tmp_path)
    seed_repo(conn, "old/name", "nid-same")  # 改名前已在池
    band = {"stars:16000..32000": {1: [make_search_item("brand/new-name", "nid-same", stars=20000)]}}  # 改名后出现
    client = FakeClient(
        nodes_by_id={"nid-same": make_node("old/name", stars=2000)},
        search_pages_by_query=band,
    )
    logger, _records = make_logger()
    stats = _run(client, conn, logger, now=MONDAY_W32)

    assert stats.discovered == 0  # 改名 ≠ 新面孔
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1
    assert conn.execute("SELECT full_name FROM repos").fetchone()["full_name"] == "old/name"  # 原名不动
    conn.close()
