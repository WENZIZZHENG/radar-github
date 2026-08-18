"""初始快照任务单测：假 Search client + tmp_path 真实 sqlite 文件，全程离线，禁真打 API。

不引 pytest-asyncio（不加新依赖）：异步用例统一 asyncio.run 驱动，与 test_github.py 同口径。
"""

import asyncio
import json

import pytest

from app.collector.snapshot import (
    MIN_STARS,
    ProgressStore,
    Shard,
    ShardSplitError,
    ingest_items,
    iter_leaf_shards,
    run_snapshot,
    split_range,
)
from app.db import get_conn, init_db


class FakeSearchClient:
    """假 Search client：按 query 脚本返回 total_count 与分页 items，全程离线。

    script 结构：{query: {"total_count": int, "top_stars": int（可选，探针首条星数）, "items": [...]（可选）}}
    per_page=1 视为探针调用，首条带 top_stars 充当"区间实际最大星数"（真实 API 按星数降序）。
    """

    def __init__(self, script: dict) -> None:
        self.script = script
        self.calls: list[tuple[str, int, int]] = []  # (query, per_page, page)

    async def search_repositories(self, query: str, *, per_page: int = 100, page: int = 1) -> dict:
        self.calls.append((query, per_page, page))
        spec = self.script[query]
        total = spec["total_count"]
        if per_page == 1:
            items = [{"stargazers_count": spec.get("top_stars", 0)}] if total else []
            return {"total_count": total, "items": items}
        items = spec.get("items") or []
        start = (page - 1) * per_page
        return {"total_count": total, "items": items[start : start + per_page]}

    def page_calls(self, query: str) -> list[tuple[str, int, int]]:
        """某 query 的分页拉取调用（per_page>1）：断点跳过分片的硬证据是不再有这类调用。"""
        return [c for c in self.calls if c[0] == query and c[1] > 1]


def make_item(full_name: str, *, stars: int = 1234, description="demo", language="Python", topics=("ai", "cli"), created_at=None) -> dict:
    """造一条 REST search item（字段对齐真实响应中入库要用的子集）。

    created_at（T-033）：GitHub 创建时间，缺省 None（真实响应必有，测试默认不给以锁定"缺失不中断"）。"""
    item = {
        "full_name": full_name,
        "description": description,
        "language": language,
        "topics": list(topics),
        "stargazers_count": stars,
        "node_id": f"node-{full_name}",
    }
    if created_at is not None:
        item["created_at"] = created_at
    return item


def collect_leaves(client: FakeSearchClient, shard: Shard) -> list:
    async def _run() -> list:
        return [leaf async for leaf in iter_leaf_shards(client, shard)]

    return asyncio.run(_run())


# ---------- 分片枚举：二分逻辑与 1000 上限红线 ----------


def test_shard_query_format():
    assert Shard(MIN_STARS, None).query == "stars:>=1000"  # 根分片无上界
    assert Shard(1000, 2000).query == "stars:1000..2000"


def test_over_cap_triggers_recursive_split():
    """total_count>1000 递归二分；DFS 高星半先跑；探针全部 per_page=1（省流量）。"""
    client = FakeSearchClient(
        {
            "stars:>=1000": {"total_count": 2500, "top_stars": 4000},
            "stars:2501..4000": {"total_count": 900},
            "stars:1000..2500": {"total_count": 1600},
            "stars:1751..2500": {"total_count": 800},
            "stars:1000..1750": {"total_count": 800},
        }
    )
    leaves = collect_leaves(client, Shard(MIN_STARS, None))
    assert [leaf.shard.query for leaf in leaves] == ["stars:2501..4000", "stars:1751..2500", "stars:1000..1750"]
    assert all(leaf.total_count <= 1000 for leaf in leaves)  # 红线：叶子分片绝不越 1000 上限
    assert {c[1] for c in client.calls} == {1}  # 枚举阶段只发探针


def test_exactly_1000_is_leaf_boundary():
    """边界值 1000：恰好顶到上限，不再二分（逐页拉取 page<=10 仍合法）。"""
    client = FakeSearchClient({"stars:>=1000": {"total_count": 1000}})
    leaves = collect_leaves(client, Shard(MIN_STARS, None))
    assert [leaf.shard.query for leaf in leaves] == ["stars:>=1000"]
    assert leaves[0].total_count == 1000
    assert len(client.calls) == 1


def test_1001_triggers_split_boundary():
    """边界值 1001：刚越上限，必须二分一次。"""
    client = FakeSearchClient(
        {
            "stars:>=1000": {"total_count": 1001, "top_stars": 2000},
            "stars:1501..2000": {"total_count": 500},
            "stars:1000..1500": {"total_count": 501},
        }
    )
    leaves = collect_leaves(client, Shard(MIN_STARS, None))
    assert [leaf.shard.query for leaf in leaves] == ["stars:1501..2000", "stars:1000..1500"]


def test_unsplittable_single_point_fails_loud():
    """退化单点（如全部仓库恰好同一星数）且仍越上限：报错指引人工细分，不静默丢数据。"""
    client = FakeSearchClient({"stars:>=1000": {"total_count": 1500, "top_stars": 1000}})
    with pytest.raises(ShardSplitError, match="二分法已到极限"):
        collect_leaves(client, Shard(MIN_STARS, None))
    with pytest.raises(ShardSplitError):
        split_range(1000, 1000)


# ---------- 入库映射：item → repos + star_snapshots ----------


def _open_db(tmp_path):
    db_path = tmp_path / "radar.db"
    init_db(db_path)
    return get_conn(db_path)


def test_ingest_maps_items_to_two_tables(tmp_path):
    conn = _open_db(tmp_path)
    items = [
        make_item("octocat/hello", stars=2000),
        make_item("octocat/plain", stars=1000, description=None, language=None, topics=()),
    ]
    result = ingest_items(conn, items, captured_at="2026-08-09T00:00:00Z", now_iso="2026-08-09T00:00:00Z")
    assert (result.repos_inserted, result.snapshots_inserted) == (2, 2)

    row = conn.execute("SELECT * FROM repos WHERE full_name = 'octocat/hello'").fetchone()
    assert row["topics"] == '["ai", "cli"]'  # schema 硬约定：JSON 数组字符串，不是 Python repr
    assert row["node_id"] == "node-octocat/hello"  # T-006 勘误补列：每日 nodes(ids:) 采集入口必须入库
    assert row["description_en"] == "demo"
    assert row["language"] == "Python"
    assert row["dead"] == 0
    assert row["source"] == "initial"
    assert row["created_at"] == "2026-08-09T00:00:00Z"

    row = conn.execute("SELECT * FROM repos WHERE full_name = 'octocat/plain'").fetchone()
    assert row["description_en"] is None  # 官方允许为空 → NULL 入库，不造空串假数据
    assert row["language"] is None
    assert row["topics"] == "[]"

    snapshots = conn.execute(
        "SELECT r.full_name, s.captured_at, s.stars FROM star_snapshots s JOIN repos r ON r.id = s.repo_id"
    ).fetchall()
    assert {(s["full_name"], s["captured_at"], s["stars"]) for s in snapshots} == {
        ("octocat/hello", "2026-08-09T00:00:00Z", 2000),
        ("octocat/plain", "2026-08-09T00:00:00Z", 1000),
    }
    conn.close()


def test_ingest_rerun_is_idempotent(tmp_path):
    """重跑同一片（如入库后、断点落盘前中断）：full_name 冲突跳过，两表都不长新行。"""
    conn = _open_db(tmp_path)
    items = [make_item("octocat/hello"), make_item("octocat/world", stars=50)]
    first = ingest_items(conn, items, captured_at="2026-08-09T00:00:00Z", now_iso="2026-08-09T00:00:00Z")
    second = ingest_items(conn, items, captured_at="2026-08-09T00:00:00Z", now_iso="2026-08-09T00:00:00Z")
    assert (first.repos_inserted, first.snapshots_inserted) == (2, 2)
    assert (second.repos_inserted, second.snapshots_inserted) == (0, 0)
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM star_snapshots").fetchone()[0] == 2
    conn.close()


def test_ingest_failure_rolls_back_whole_shard(tmp_path):
    """分片中途失败（item 缺 stargazers_count）：repos 写入必须整体回滚，不留半片脏数据。"""
    conn = _open_db(tmp_path)
    bad_item = make_item("octocat/broken")
    del bad_item["stargazers_count"]
    with pytest.raises(KeyError):
        ingest_items(conn, [bad_item], captured_at="2026-08-09T00:00:00Z", now_iso="2026-08-09T00:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM star_snapshots").fetchone()[0] == 0
    conn.close()


def test_ingest_stores_github_created_at_normalized(tmp_path):
    """T-033：Search item 的 created_at 归一化入 repos.github_created_at（带毫秒变体剥毫秒为定长）；
    缺失 → NULL 且不中断（采集主链路不因展示字段断）。"""
    conn = _open_db(tmp_path)
    items = [
        make_item("octocat/hello", stars=2000, created_at="2024-01-15T08:30:00.123Z"),
        make_item("octocat/plain", stars=1000),  # 无 created_at
        make_item("octocat/weird", stars=1500, created_at="not-a-date"),  # 畸形 → NULL 不中断
    ]
    result = ingest_items(conn, items, captured_at="2026-08-09T00:00:00Z", now_iso="2026-08-09T00:00:00Z")
    assert result.repos_inserted == 3
    rows = {
        r["full_name"]: r["github_created_at"]
        for r in conn.execute("SELECT full_name, github_created_at FROM repos")
    }
    assert rows["octocat/hello"] == "2024-01-15T08:30:00Z"  # 毫秒剥除归一化（schema 定长硬约定）
    assert rows["octocat/plain"] is None  # 缺失存 NULL
    assert rows["octocat/weird"] is None  # 畸形存 NULL
    conn.close()


# ---------- 断点续跑：中断后重跑，已完成分片跳过 ----------


def _two_leaf_script() -> dict:
    return {
        "stars:>=1000": {"total_count": 1500, "top_stars": 2000},
        "stars:1501..2000": {"total_count": 600, "items": [make_item("a/one"), make_item("a/two")]},
        "stars:1000..1500": {"total_count": 900, "items": [make_item("b/three")]},
    }


def _run_once(client, conn, progress, **kwargs):
    return asyncio.run(run_snapshot(client, conn, progress, **kwargs))


def test_resume_skips_completed_shards(tmp_path):
    progress_path = tmp_path / "progress.json"

    # 第一轮：只跑枚举顺序第 1 片（等价 CLI --max-shards 1）
    conn = _open_db(tmp_path)
    stats1 = _run_once(FakeSearchClient(_two_leaf_script()), conn, ProgressStore(progress_path), max_shards=1)
    assert stats1.shards_done == 1
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 2  # 高星半先跑：a/one、a/two 已入库

    saved = json.loads(progress_path.read_text(encoding="utf-8"))
    assert set(saved["completed"]) == {"stars:1501..2000"}  # 断点已落盘
    assert saved["completed"]["stars:1501..2000"]["repos_inserted"] == 2

    # 第二轮：重跑同一命令——第 1 片被跳过且窗口用尽即停，不向后推进，库内行数不变（幂等硬证据）
    client2 = FakeSearchClient(_two_leaf_script())
    stats2 = _run_once(client2, conn, ProgressStore(progress_path), max_shards=1)
    assert stats2.shards_done == 0 and stats2.shards_skipped == 1
    assert client2.page_calls("stars:1501..2000") == []  # 已完成分片零分页请求
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 2

    # 第三轮：窗口放宽到前 2 片——第 1 片跳过，接着跑第 2 片
    client3 = FakeSearchClient(_two_leaf_script())
    stats3 = _run_once(client3, conn, ProgressStore(progress_path), max_shards=2)
    assert stats3.shards_skipped == 1 and stats3.shards_done == 1
    assert client3.page_calls("stars:1501..2000") == [] and len(client3.page_calls("stars:1000..1500")) > 0
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 3

    # 第四轮：全部完成——两片都跳过，库内行数不变
    client4 = FakeSearchClient(_two_leaf_script())
    stats4 = _run_once(client4, conn, ProgressStore(progress_path))
    assert stats4.shards_done == 0 and stats4.shards_skipped == 2
    assert client4.page_calls("stars:1501..2000") == [] and client4.page_calls("stars:1000..1500") == []
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM star_snapshots").fetchone()[0] == 3
    conn.close()


def test_progress_store_rejects_corrupt_file(tmp_path):
    """断点文件损坏：fail loud 指向处置方式，不静默当全量重跑（虽然幂等但浪费配额）。"""
    bad = tmp_path / "progress.json"
    bad.write_text("{不是合法JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="不是有效 JSON"):
        ProgressStore(bad)


def test_captured_at_shared_within_shard(tmp_path):
    """同一分片所有快照共享一个 captured_at（同批一致）；now_iso 逐次变值以放大差异。"""
    ticks = iter(["2026-08-09T00:00:01Z", "2026-08-09T00:00:02Z", "2026-08-09T00:00:03Z"])
    script = {
        "stars:>=1000": {
            "total_count": 3,
            "items": [make_item("a/one"), make_item("a/two"), make_item("a/three")],
        }
    }
    conn = _open_db(tmp_path)
    stats = _run_once(
        FakeSearchClient(script), conn, ProgressStore(tmp_path / "progress.json"), now_iso=lambda: next(ticks)
    )
    assert stats.shards_done == 1
    values = {row[0] for row in conn.execute("SELECT captured_at FROM star_snapshots")}
    assert values == {"2026-08-09T00:00:01Z"}
    assert {row[0] for row in conn.execute("SELECT created_at FROM repos")} == {"2026-08-09T00:00:01Z"}
    conn.close()



# ---------- 评审 findings 修复锁定 ----------


def test_fetch_shard_items_accumulates_multiple_pages(tmp_path):
    """多页拉取：第 2 页起的内容必须真实累积并入库（防 extend 路径假绿——total_count=150 走两页）。"""
    items_150 = [make_item(f"o/r{i:03d}", stars=1500) for i in range(150)]
    script = {"stars:>=1000": {"total_count": 150, "items": items_150}}
    conn = _open_db(tmp_path)
    stats = _run_once(FakeSearchClient(script), conn, ProgressStore(tmp_path / "progress.json"))
    assert stats.shards_done == 1
    assert stats.repos_inserted == 150  # 两页（100+50）内容全部入库
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 150
    conn.close()


def test_root_probe_missing_items_fails_loud():
    """根分片探针响应缺 items（total 越上限但无法定上界）：fail loud 指明分片，不静默。"""

    class BrokenProbeClient:
        async def search_repositories(self, query, *, per_page=100, page=1):
            return {"total_count": 1500, "items": []}

    with pytest.raises(ShardSplitError, match="无法为"):
        collect_leaves(BrokenProbeClient(), Shard(MIN_STARS, None))


def test_drift_alert_emitted_when_items_deviate(tmp_path):
    """拉取数与探针 total_count 偏差超阈值：打出告警行（漂移留痕不修数据）；阈值内不告警。"""
    items_5 = [make_item(f"o/r{i}", stars=1500) for i in range(5)]
    # total_count=100、实际只拉到 5 条（模拟仓库大量移出区间）：偏差 95 > max(10, 100//10) 触发告警
    script = {"stars:>=1000": {"total_count": 100, "items": items_5}}
    conn = _open_db(tmp_path)
    logs: list[str] = []
    _run_once(FakeSearchClient(script), conn, ProgressStore(tmp_path / "progress.json"), log=logs.append)
    assert any("[告警]" in line for line in logs)

    # 阈值内不告警：total_count=13、拉满 13 条，零偏差
    items_13 = [make_item(f"p/r{i}", stars=1500) for i in range(13)]
    script2 = {"stars:>=1000": {"total_count": 13, "items": items_13}}
    conn2 = _open_db(tmp_path / "db2")
    logs2: list[str] = []
    _run_once(FakeSearchClient(script2), conn2, ProgressStore(tmp_path / "progress2.json"), log=logs2.append)
    assert not any("[告警]" in line for line in logs2)
    conn.close()
    conn2.close()
