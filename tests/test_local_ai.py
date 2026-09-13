"""local-ai-relay 测试：导出判定（与每日 ensure 同口径）＋ 作业单两形态 ＋ 回填校验/清洗/写入 ＋ 两个新端点。

口径钉死：判定顺序 translate → week → quarter → total → summary；S = 三口径榜去重 ∪ 关注集（_scope_sets 同源）；
`source='manual'` 的行不作重生候选；回填默认只补缺失、一律写 manual；期次过期整条拒绝、坏条不阻断同批。
全程离线（假 GitHub client / dependency_overrides），export 与 fill 都不依赖 AI client（AI_ENABLED=0 下可用）。
"""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.collector.github import GitHubAuthError
from app.db import get_conn, init_db
from app.local_ai import (
    MAX_TEXT_CHARS,
    apply_fill,
    build_export,
    clean_fill_text,
    parse_fill_payload,
    render_export_text,
    text_fingerprint,
)
from app.main import app
from app.web.routes import _ai_client, _github_client

AS_OF_DT = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)  # 周日 12:00，所在 ISO 周 = 2026-W32（非窗口日）
WEEK1 = "2026-W32"
WEEK2 = "2026-W33"  # 2026-08-15（半月窗口日）所在周
QUARTER1 = "2026-Q3"
WINDOW_DAY = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)  # 每月 1/15 号才开窗


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _current_week_label() -> str:
    """真实今天所在 ISO 周标签（接口用例走真实 now，断言与之对齐）。"""
    iso = datetime.now(timezone.utc).date().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class FakeGitHubClient:
    """假 GitHub client：fetch_readme 按 full_name 返回 (text, sha)；可注入普通失败与账户类错误。"""

    def __init__(self, *, readmes=None, fail_for=(), auth_for=()):
        self.readmes = readmes or {}
        self.fail_for = set(fail_for)
        self.auth_for = set(auth_for)
        self.readme_calls: list[str] = []

    async def fetch_readme(self, full_name: str) -> tuple[str | None, str | None]:
        self.readme_calls.append(full_name)
        if full_name in self.auth_for:
            raise GitHubAuthError("GitHub token 无效或已过期")
        if full_name in self.fail_for:
            raise RuntimeError("模拟 README 拉取失败")
        return self.readmes.get(full_name, (None, None))

    async def aclose(self) -> None:
        pass


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "local_ai.db"
    init_db(db)
    c = get_conn(db)
    yield c
    c.close()


# ---------- 造数助手（口径照 tests/test_ai.py） ----------


def _add_repo(
    conn,
    name,
    *,
    description_en="english text",
    language="Python",
    description_zh=None,
    listed=True,
    base=1000,
    delta=100,
    quarter=False,
    now=AS_OF_DT,
):
    """插仓库＋周窗口两端快照（listed=False 只插端点一张）；quarter=True 再加 87 天前一张（季榜出席）。"""
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at)"
        " VALUES (?, ?, ?, ?, ?, '[]', 0, 'test', '2026-07-01T00:00:00Z')",
        (name, f"node-{name}", description_en, description_zh, language),
    )
    repo_id = cur.lastrowid
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
        (repo_id, _iso(now), base + delta),
    )
    if listed:
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, _iso(now - timedelta(days=7)), base),
        )
    if quarter:
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (repo_id, _iso(now - timedelta(days=87)), base),
        )
    conn.commit()
    return repo_id


def _insert_row(conn, repo_id, dimension, period_label, text, *, sha=None, generated_week=WEEK1, source="ai"):
    conn.execute(
        "INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (repo_id, dimension, period_label, text, sha, generated_week, source),
    )
    conn.commit()


def _run_export(conn, *, limit=20, kind="all", github=None, now=AS_OF_DT):
    return asyncio.run(build_export(conn, limit=limit, kind=kind, github_client=github, now=now))


def _item(payload, kind, repo):
    return next(item for item in payload["items"] if item["kind"] == kind and item["repo"] == repo)


# ---------- 导出判定 ----------


def test_export_non_window_day_lists_missing_only(conn):
    """非窗口日：只列缺失（translate/quarter/summary），已有行的 week/total 不出任务；响应带期次与窗口态。"""
    _add_repo(conn, "a/one")
    repo_id = 1
    _insert_row(conn, repo_id, "week", WEEK1, "已有周文本")
    _insert_row(conn, repo_id, "total", "all", "已有总星文本", sha="sha-1")
    payload = _run_export(conn, github=FakeGitHubClient())

    assert payload["as_of"] == "2026-08-09T12:00:00Z"
    assert payload["week_label"] == WEEK1 and payload["quarter_label"] == QUARTER1
    assert payload["window_open"] is False and payload["probe_truncated"] is False
    assert [item["kind"] for item in payload["items"]] == ["translate", "quarter", "summary"]  # 判定顺序同跑批
    assert payload["remaining"] == 0
    # 每条任务字段齐备：task_id/kind/repo/dimension/period_label/system/user
    translate = _item(payload, "translate", "a/one")
    assert translate["task_id"] == "T1" and translate["dimension"] is None and translate["period_label"] is None
    assert "翻译成自然简洁的中文" in translate["system"] and translate["user"] == "english text"
    quarter = _item(payload, "quarter", "a/one")
    assert quarter["dimension"] == "quarter" and quarter["period_label"] == QUARTER1
    assert "季榜上榜项目的推荐理由" in quarter["system"]  # 季维度 prompt（新区语境：无 90 天快照）
    summary = _item(payload, "summary", "a/one")
    assert summary["dimension"] == "summary" and summary["period_label"] == "all"
    assert "AI 概要" in summary["system"]


def test_export_window_day_regenerates_ai_row_but_not_manual(conn):
    """窗口日：ai 行 README sha 变化 → 出重生任务；同条件的 manual 行不出任务（人工行豁免）。"""
    _add_repo(conn, "a/ai")
    _add_repo(conn, "a/manual")
    _insert_row(conn, 1, "total", "all", "ai 总星文本", sha="sha-v1")
    _insert_row(conn, 2, "total", "all", "人工总星文本", sha="sha-v1", source="manual")
    github = FakeGitHubClient(
        readmes={"a/ai": ("v2 readme", "sha-v2"), "a/manual": ("v2 readme", "sha-v2")}
    )
    payload = _run_export(conn, github=github, now=WINDOW_DAY)

    assert payload["window_open"] is True
    total_repos = {item["repo"] for item in payload["items"] if item["kind"] == "total"}
    assert total_repos == {"a/ai"}
    assert _item(payload, "total", "a/ai")["period_label"] == "all"


def test_export_manual_row_excluded_but_missing_dimension_still_exported(conn):
    """窗口日人工行：既不作为重生候选，也不影响同仓缺失维度照常导出（缺失维度仍出任务）。"""
    _add_repo(conn, "a/one")
    _insert_row(conn, 1, "total", "all", "人工总星文本", sha="sha-v1", source="manual")
    github = FakeGitHubClient(readmes={"a/one": ("v2 readme", "sha-v2")})
    payload = _run_export(conn, github=github, now=WINDOW_DAY)

    kinds = [item["kind"] for item in payload["items"] if item["repo"] == "a/one"]
    assert kinds == ["translate", "week", "quarter", "summary"]  # 该仓别处仍缺（窗口日跨到 W33）
    assert "total" not in kinds  # 人工 total 行不出重生任务


def test_export_quarter_window_regeneration_skips_manual(conn):
    """窗口日季榜：ai 行 generated_week ≠ 当周 → 出重生任务；manual 行同条件 → 不出任务（非窗口日都不出）。"""
    _add_repo(conn, "a/ai", quarter=True)
    _add_repo(conn, "a/manual", quarter=True)
    _insert_row(conn, 1, "quarter", QUARTER1, "ai 季榜文本", generated_week=WEEK1)
    _insert_row(conn, 2, "quarter", QUARTER1, "人工季榜文本", generated_week=WEEK1, source="manual")
    payload = _run_export(conn, github=FakeGitHubClient(), now=WINDOW_DAY)

    assert {item["repo"] for item in payload["items"] if item["kind"] == "quarter"} == {"a/ai"}
    # 非窗口日：ai 行也不重生（懒口径），全批无 quarter 类任务
    payload2 = _run_export(conn, github=FakeGitHubClient(), now=AS_OF_DT)
    assert [item for item in payload2["items"] if item["kind"] == "quarter"] == []


def test_export_quarter_window_same_week_row_skipped(conn):
    """窗口日但既有行 generated_week == 当周：无 quarter 类任务（与 recommend_missing 守卫同口径）。"""
    _add_repo(conn, "a/one", quarter=True)
    _insert_row(conn, 1, "quarter", QUARTER1, "本周已生成", generated_week=WEEK2)
    payload = _run_export(conn, github=FakeGitHubClient(), now=WINDOW_DAY)

    kinds = [item["kind"] for item in payload["items"]]
    assert "quarter" not in kinds and kinds == ["translate", "week", "total", "summary"]  # 其余缺失维度照常


def test_export_week_missing_and_manual_row_not_duplicated(conn):
    """周榜：缺当周行则出任务；已有当周行（含人工行）不出任务（每期次新行语义不变）。"""
    _add_repo(conn, "a/one")
    _add_repo(conn, "a/manual")
    _insert_row(conn, 2, "week", WEEK1, "人工周文本", source="manual")
    payload = _run_export(conn, github=FakeGitHubClient())

    assert {item["repo"] for item in payload["items"] if item["kind"] == "week"} == {"a/one"}


def test_export_translate_conditions(conn):
    """翻译段判定与 ensure 同口径：已有中文/空简介/已译不出任务，仅"英文非空且不含 CJK 且未译"出任务。"""
    _add_repo(conn, "a/cjk", description_en="已有中文描述的项目")
    _add_repo(conn, "a/none", description_en=None)
    _add_repo(conn, "a/done", description_en="already translated", description_zh="已译")
    _add_repo(conn, "a/todo", description_en="needs translation")
    payload = _run_export(conn, kind="translate", github=FakeGitHubClient())

    assert [item["repo"] for item in payload["items"]] == ["a/todo"]
    assert payload["items"][0]["user"] == "needs translation"
    assert payload["remaining"] == 0


def test_export_kind_filter(conn):
    """kind 过滤：只产指定类别任务，其余类别不出现。"""
    _add_repo(conn, "a/one", quarter=True)
    for kind in ("translate", "week", "quarter", "total", "summary"):
        payload = _run_export(conn, kind=kind, github=FakeGitHubClient())
        assert {item["kind"] for item in payload["items"]} <= {kind}
    payload_week = _run_export(conn, kind="week", github=FakeGitHubClient())
    assert [item["kind"] for item in payload_week["items"]] == ["week"]
    assert payload_week["remaining"] == 0


def test_export_limit_truncates_and_reports_remaining(conn):
    """limit 截断：恰好 limit 条 + remaining = 廉价待办总数 − 本次导出数（kind 过滤同样计入）。"""
    for i in range(5):
        _add_repo(conn, f"f/r{i}", description_en=f"desc {i}")
    payload = _run_export(conn, limit=2, kind="translate", github=FakeGitHubClient())

    assert [item["task_id"] for item in payload["items"]] == ["T1", "T2"]
    assert payload["remaining"] == 3  # 5 条待译 − 本次 2 条
    all_kinds = _run_export(conn, limit=50, kind="translate", github=FakeGitHubClient())
    assert len(all_kinds["items"]) == 5 and all_kinds["remaining"] == 0


def test_export_probe_limit_guard_on_window_day(conn):
    """窗口日探测护栏：已有行是否重生需拉 README——**每段**探测上限 = min(max(limit*8, 200), 500)，
    达上限后不再探测该候选（probe_truncated/probe_skipped 如实上报），且不因探测无果而无限拉 README。"""
    total = 240  # > 保底上限 200：limit=10 时 budget = max(80, 200) = 200
    for i in range(total):
        repo_id = _add_repo(conn, f"f/r{i:03d}")
        conn.execute(
            "INSERT INTO follows (repo_id, created_at) VALUES (?, '2026-07-02T00:00:00Z')", (repo_id,)
        )
        _insert_row(conn, repo_id, "week", WEEK2, "已有周文本", generated_week=WEEK2)
        _insert_row(conn, repo_id, "quarter", QUARTER1, "已有季文本", generated_week=WEEK2)
        _insert_row(conn, repo_id, "total", "all", "已有总星文本", sha="sha-same", generated_week=WEEK2)
        _insert_row(conn, repo_id, "summary", "all", "已有概要文本", sha="sha-same", generated_week=WEEK2)
        conn.execute("UPDATE repos SET description_zh = '已译' WHERE id = ?", (repo_id,))
    conn.commit()
    github = FakeGitHubClient(readmes={f"f/r{i:03d}": ("readme", "sha-same") for i in range(total)})

    payload = _run_export(conn, limit=10, github=github, now=WINDOW_DAY)

    assert payload["items"] == []  # 全部行齐备且指纹未变：无任务
    assert payload["probe_truncated"] is True  # 240 个候选 > 上限 200：本轮提前结束探测
    assert len(github.readme_calls) == 200  # min(max(10*8, 200), 500) = 200
    assert payload["probe_skipped"] == 80  # total 段跳过 40 ＋ summary 段（独立预算）跳过 40
    assert payload["remaining"] == 0  # 重生态不是廉价判定：不计入 remaining


def test_export_probe_budget_per_section_not_starved(conn):
    """F2-1 回归：探测预算**按段独立**——total 段吃满预算也不饿死 summary 段。

    评审复现场景：210 个关注仓行齐备、指纹未变，仅位次第 3 的仓 summary 指纹已变；limit=10 时
    budget=200 → total 段探测 200 次后耗尽并跳过其余 10 个；summary 段持有自己的 200 次预算，
    该变更仓必须照常出任务（共享计数器时 summary 段零判定、items 为空、剩下的变更仓永远看不到）。"""
    total = 210
    for i in range(total):
        repo_id = _add_repo(conn, f"f/r{i:03d}")
        conn.execute("INSERT INTO follows (repo_id, created_at) VALUES (?, '2026-07-02T00:00:00Z')", (repo_id,))
        _insert_row(conn, repo_id, "week", WEEK2, "已有周文本", generated_week=WEEK2)
        _insert_row(conn, repo_id, "quarter", QUARTER1, "已有季文本", generated_week=WEEK2)
        _insert_row(  # 第 3 个仓的 total 指纹已跟上、summary 指纹已过期（只有 summary 该重生）
            conn,
            repo_id,
            "total",
            "all",
            "已有总星文本",
            sha="sha-changed" if i == 2 else "sha-same",
            generated_week=WEEK2,
        )
        _insert_row(conn, repo_id, "summary", "all", "已有概要文本", sha="sha-same", generated_week=WEEK2)
        conn.execute("UPDATE repos SET description_zh = '已译' WHERE id = ?", (repo_id,))
    conn.commit()
    github = FakeGitHubClient(
        readmes={f"f/r{i:03d}": ("readme", "sha-changed" if i == 2 else "sha-same") for i in range(total)}
    )

    payload = _run_export(conn, limit=10, github=github, now=WINDOW_DAY)

    assert [(item["kind"], item["repo"]) for item in payload["items"]] == [("summary", "f/r002")]
    assert payload["probe_skipped"] == 20  # total 段 10 ＋ summary 段 10（两段各自独立耗尽）
    assert payload["probe_truncated"] is True
    assert len(github.readme_calls) == 200  # 每段各 200 次判定；summary 段命中 total 段已拉的缓存


def test_export_reports_probe_skipped_when_limit_fills_sections(conn):
    """F2-2' 回归：窗口日 limit 被 total 段凑满提前结束时，summary 段（与 total 尾部）"本该探测而未判定"
    的候选必须计入 probe_skipped / probe_truncated——否则调用方看到 remaining=0 就停下，漏掉概要与 total 尾部重生。

    复现评审场景：240 关注仓、limit=10、每个仓 total 指纹都已变（前 10 条凑满 limit）、位次第 3 的仓
    summary 指纹也已变（在 total 段 break 之后——summary 段整段没跑）。尾部另放两类边界候选（评审 F3-d）：
    人工 total 行（source='manual'，豁免重生）与缺 total 行的仓（归 remaining）——两者都不许计入尾扫。
    跟随顺序逐仓递增（断言可复现），最终精确值：probe_skipped = total 尾扫 230 ＋ summary 整段 242 = 472。"""
    total = 240
    for i in range(total):
        repo_id = _add_repo(conn, f"f/r{i:03d}")
        conn.execute(  # 逐仓递增的跟随时刻：关注序确定，不发生并列导致断言漂移
            "INSERT INTO follows (repo_id, created_at) VALUES (?, ?)",
            (repo_id, f"2026-07-02T{i // 60:02d}:{i % 60:02d}:00Z"),
        )
        _insert_row(conn, repo_id, "week", WEEK2, "已有周文本", generated_week=WEEK2)
        _insert_row(conn, repo_id, "quarter", QUARTER1, "已有季文本", generated_week=WEEK2)
        _insert_row(conn, repo_id, "total", "all", "已有总星文本", sha="sha-old", generated_week=WEEK2)
        _insert_row(  # 只有 f/r002 的概要指纹过期（其余仓 total 过期、概要已跟上）
            conn, repo_id, "summary", "all", "已有概要文本",
            sha="sha-same" if i == 2 else "sha-new", generated_week=WEEK2,
        )
        conn.execute("UPDATE repos SET description_zh = '已译' WHERE id = ?", (repo_id,))
    # 排在全部常规仓之后的两个边界仓：人工 total 行 / 缺 total 行（都不计入 probe_skipped）
    manual_id = _add_repo(conn, "f/manual")
    conn.execute("INSERT INTO follows (repo_id, created_at) VALUES (?, '2026-07-03T00:00:00Z')", (manual_id,))
    _insert_row(conn, manual_id, "week", WEEK2, "已有周文本", generated_week=WEEK2)
    _insert_row(conn, manual_id, "quarter", QUARTER1, "已有季文本", generated_week=WEEK2)
    _insert_row(conn, manual_id, "total", "all", "人工总星文本", sha="sha-old", generated_week=WEEK2, source="manual")
    _insert_row(conn, manual_id, "summary", "all", "已有概要文本", sha="sha-new", generated_week=WEEK2)
    conn.execute("UPDATE repos SET description_zh = '已译' WHERE id = ?", (manual_id,))
    missing_id = _add_repo(conn, "f/missing")
    conn.execute("INSERT INTO follows (repo_id, created_at) VALUES (?, '2026-07-03T00:01:00Z')", (missing_id,))
    _insert_row(conn, missing_id, "week", WEEK2, "已有周文本", generated_week=WEEK2)
    _insert_row(conn, missing_id, "quarter", QUARTER1, "已有季文本", generated_week=WEEK2)
    _insert_row(conn, missing_id, "summary", "all", "已有概要文本", sha="sha-new", generated_week=WEEK2)
    conn.execute("UPDATE repos SET description_zh = '已译' WHERE id = ?", (missing_id,))
    conn.commit()
    readmes = {f"f/r{i:03d}": ("readme", "sha-new") for i in range(total)}
    github = FakeGitHubClient(readmes={**readmes, "f/manual": ("readme", "sha-new"), "f/missing": ("readme", "sha-new")})

    payload = _run_export(conn, limit=10, github=github, now=WINDOW_DAY)

    assert [item["kind"] for item in payload["items"]] == ["total"] * 10  # 本批全是 total（先跑且先凑满）
    assert payload["remaining"] == 1  # f/missing 缺 total 行：归 remaining，不归 probe_skipped（F3-d）
    assert payload["probe_truncated"] is True  # 未判定信号必须出现
    assert payload["probe_skipped"] == (total - 10) + (total + 2)  # 230 ＋ 242 = 472
    assert len(github.readme_calls) == 10  # 只判定了前 10 个（各拉一次）；尾扫零 I/O

    # 对照：kind=summary 单独导能拿到那条概要重生（证明未判定确实存在、且绕道可用）
    summary_only = _run_export(conn, limit=10, kind="summary", github=github, now=WINDOW_DAY)
    assert [(item["kind"], item["repo"]) for item in summary_only["items"]] == [("summary", "f/r002")]


def test_export_readme_failure_degrades_to_metadata(conn):
    """README 拉取失败：任务照常导出（退化元数据输入），user prompt 不含 README 段落，不抛出。"""
    _add_repo(conn, "a/one", quarter=True)
    github = FakeGitHubClient(fail_for={"a/one"})
    payload = _run_export(conn, kind="week", github=github)

    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert "README 要点" not in item["user"]
    assert item["user"].startswith("仓库：a/one\n简介：english text\n主语言：Python")


def test_export_includes_readme_head_in_prompt(conn):
    """README 正文按 AI_README_HEAD_CHARS 截断后进 prompt（与 AI 路径同口径）。"""
    _add_repo(conn, "a/one")
    github = FakeGitHubClient(readmes={"a/one": ("# One\n\n" + "x" * 10000, "sha-1")})
    payload = _run_export(conn, kind="week", github=github)

    assert "README 要点：\n# One\n\n" + "x" * (8000 - 9) in payload["items"][0]["user"]


def test_export_honors_readme_head_chars_env(conn, monkeypatch):
    """README 截断走 .env AI_README_HEAD_CHARS（不是模块常量）：env=5 时 user 段只含前 5 个字符。

    回归意义（评审 F3-2）：生产该键为 3000、代码缺省 8000；若接线从 get_settings() 退化成模块常量，
    本用例变红——否则 500+ 条用例全绿而生产 prompt 体量翻倍也无人发现。"""
    monkeypatch.setenv("AI_README_HEAD_CHARS", "5")
    _add_repo(conn, "a/one")
    github = FakeGitHubClient(readmes={"a/one": ("# One-long-readme-body", "sha-1")})
    payload = _run_export(conn, kind="week", github=github)

    user = _item(payload, "week", "a/one")["user"]
    assert "README 要点：\n# One\n" in user  # 截断到 5 字符（"# One"）后紧接下一行
    assert "long-readme-body" not in user


def test_export_is_read_only(conn):
    """导出只读回归（评审 F3-3）：同连接 total_changes 与关键表行数前后一律不变——
    将来有人在导出里加写库/落探测游标，本用例必红。"""
    _add_repo(conn, "a/one", quarter=True)
    _insert_row(conn, 1, "total", "all", "已有总星文本", sha="sha-old", generated_week=WEEK2)
    tables = ("repos", "star_snapshots", "follows", "recommendations")
    before_changes = conn.total_changes
    before_rows = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    github = FakeGitHubClient(readmes={"a/one": ("# One", "sha-new")})

    payload = _run_export(conn, github=github, now=WINDOW_DAY)  # 窗口日：含探测与 README 拉取

    assert _item(payload, "total", "a/one")  # 确实走到探测并产出重生任务
    assert github.readme_calls == ["a/one"]
    assert conn.total_changes == before_changes
    assert {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables} == before_rows


def test_export_render_text_sheet_self_contained(conn):
    """format=text 作业单：含导出时刻/期次、输出格式要求（四个字段名）、逐条 system+user 全文；
    任务集合与 json 形态一致（同一 payload 渲染）。"""
    _add_repo(conn, "a/one", quarter=True)
    payload = _run_export(conn, kind="all", github=FakeGitHubClient())
    text = render_export_text(payload)

    assert "导出时刻：2026-08-09T12:00:00Z" in text
    assert f"周 {WEEK1}｜季 {QUARTER1}" in text
    assert '"items"' in text and '"repo"' in text and '"kind"' in text and '"period_label"' in text
    assert "推荐理由：" in text  # 清洗前缀提示（要求本地工具不要加）
    assert f"任务条数：{len(payload['items'])}" in text
    for index, item in enumerate(payload["items"], 1):
        assert f"【任务 {index}/{len(payload['items'])}】" in text
        assert f"回填字段：repo={item['repo']}｜kind={item['kind']}" in text
        assert item["system"] in text and item["user"] in text  # system/user 全文逐字进作业单


def test_export_render_text_sheet_empty(conn):
    """无待办时也给出输出格式要求与"没有待生成任务"提示（作业单形态稳定，便于脚本消费）。"""
    text = render_export_text(_run_export(conn, github=FakeGitHubClient()))
    assert "本次没有待生成任务。" in text
    assert '"items"' in text


def test_export_render_text_sheet_probe_hint_gives_skipped_and_next_step():
    """作业单在存在未判定候选时给出 probe_skipped 数与推进方式：文案用"候选判定（同仓总星/概要各算一次）"
    口径（评审 F3-8：原写"仓库"与按段累加的计数不符），并说清"先回填本批再导出下一批"。"""
    payload = {
        "as_of": "2026-08-15T00:00:00Z",
        "week_label": WEEK2,
        "quarter_label": QUARTER1,
        "window_open": True,
        "remaining": 0,
        "probe_truncated": True,
        "probe_skipped": 7,
        "items": [],
    }
    text = render_export_text(payload)

    assert "另有 7 个候选判定未做" in text
    assert "同一仓库的总星/概要各算一次" in text
    assert "该数字是告警值、不是进度" in text  # F2-c 口径：不是剩余工作量，重复导出不清零
    assert "收工判据＝本次导出里没有任何 total/summary 重生任务" in text
    assert "已回填的行是人工行，不再占用探测预算" in text
    # 未发生未判定时不出现该提示（默认档作业单不吓人）
    assert "另有 7 个候选判定未做" not in render_export_text({**payload, "probe_truncated": False, "probe_skipped": 0})


# ---------- 回填：解析与清洗 ----------


def test_parse_fill_payload_shapes():
    """外层两形态（对象带 overwrite / 裸数组）＋ markdown 围栏与前后夹带文字的容错。"""
    item = '{"repo": "a/one", "kind": "total", "period_label": "all", "text": "正文"}'
    items, overwrite = parse_fill_payload('{"items": [' + item + '], "overwrite": true}')
    assert len(items) == 1 and overwrite is True
    items, overwrite = parse_fill_payload("[" + item + "]")
    assert isinstance(items, list) and len(items) == 1 and overwrite is False  # 裸数组等价 overwrite=false
    items, overwrite = parse_fill_payload("```json\n[" + item + "]\n```")
    assert len(items) == 1 and overwrite is False
    items, _ = parse_fill_payload("好的，以下是结果：\n[" + item + "]\n请查收")
    assert len(items) == 1
    items, _ = parse_fill_payload(json.dumps({"items": [json.loads(item)]}).encode("utf-8"))
    assert len(items) == 1  # bytes 入参（路由层读原始 body）


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "  ",
        "not json",
        '{"overwrite": true}',  # 缺 items
        '{"items": "oops"}',
        '{"items": [], "overwrite": "yes"}',  # overwrite 非布尔
        '"只是个字符串"',
    ],
)
def test_parse_fill_payload_rejects_bad_input(raw):
    """非法请求体一律 ValueError（路由层映射 400 带可操作文案）。"""
    with pytest.raises(ValueError):
        parse_fill_payload(raw)


def test_clean_fill_text_strips_shell_only():
    """最小清洗：去首尾空白、成对引号（中英文）、前缀（含半角冒号变体）；正文内部一字不动。"""
    assert clean_fill_text("  正文  ") == "正文"
    assert clean_fill_text("推荐理由：这是一个数据管道工具。") == "这是一个数据管道工具。"
    assert clean_fill_text("推荐语: 值得关注。") == "值得关注。"
    assert clean_fill_text("译文：中文译文") == "中文译文"
    assert clean_fill_text("AI 概要：这是一个项目") == "这是一个项目"  # 概要 prompt 钉死的禁前缀（评审 F3-5）
    assert clean_fill_text("AI 概要: 这是一个项目") == "这是一个项目"
    assert clean_fill_text('"带引号的正文"') == "带引号的正文"
    assert clean_fill_text("“中文引号正文”") == "中文引号正文"
    assert clean_fill_text("「推荐理由：“双层壳”」") == "双层壳"
    assert clean_fill_text('他说"你好"就完了') == '他说"你好"就完了'  # 非成对包裹：不动
    assert clean_fill_text("“只一个左引号") == "“只一个左引号"
    assert clean_fill_text("推荐理由：") == ""  # 空壳：调用方按格式错误拒绝


# ---------- 回填：写入语义 ----------


def _run_fill(conn, items, *, overwrite=False, github=None, now=AS_OF_DT):
    return asyncio.run(
        apply_fill(conn, items=items, overwrite=overwrite, github_client=github, now=now)
    )


def _rec_rows(conn, repo_id=1):
    return [
        (r["dimension"], r["period_label"], r["text"], r["readme_sha"], r["generated_week"], r["source"])
        for r in conn.execute(
            "SELECT dimension, period_label, text, readme_sha, generated_week, source FROM recommendations"
            " WHERE repo_id = ? ORDER BY dimension, period_label",
            (repo_id,),
        )
    ]


def _week_item(repo, text, *, period_label=WEEK1):
    return {"repo": repo, "kind": "week", "period_label": period_label, "text": text}


def _translate_item(repo, text, *, src, period_label=None):
    """translate 回填条目：src＝导出时的原文指纹（必填，缺失/过期一律拒收）。"""
    return {"repo": repo, "kind": "translate", "period_label": period_label, "src": src, "text": text}


def test_fill_writes_manual_week_row_and_skips_existing(conn):
    """默认只补缺失：已有行 skipped 且原样不动；缺失行写入（generated_week=当周、readme_sha=NULL、manual）。"""
    _add_repo(conn, "a/one")
    _add_repo(conn, "a/two")
    _insert_row(conn, 1, "week", WEEK1, "旧周文本")
    result = _run_fill(conn, [_week_item("a/one", "新周文本"), _week_item("a/two", "二周文本")])

    assert result == {"written": 1, "skipped": 1, "failed": 0, "errors": []}
    assert _rec_rows(conn, 1) == [("week", WEEK1, "旧周文本", None, WEEK1, "ai")]
    assert _rec_rows(conn, 2) == [("week", WEEK1, "二周文本", None, WEEK1, "manual")]


def test_fill_overwrite_true_replaces_and_marks_manual(conn):
    """overwrite=true：覆盖既有行（INSERT OR REPLACE），source 变 manual；未开 overwrite 不覆盖。"""
    _add_repo(conn, "a/one")
    _insert_row(conn, 1, "total", "all", "旧总星文本", sha="sha-old")
    github = FakeGitHubClient(readmes={"a/one": ("readme", "sha-new")})

    assert _run_fill(conn, [{"repo": "a/one", "kind": "total", "period_label": "all", "text": "补缺跳过"}]) == {
        "written": 0,
        "skipped": 1,
        "failed": 0,
        "errors": [],
    }
    result = _run_fill(
        conn,
        [{"repo": "a/one", "kind": "total", "period_label": "all", "text": "人工总星文本"}],
        overwrite=True,
        github=github,
    )
    assert result["written"] == 1 and result["errors"] == []
    assert _rec_rows(conn, 1) == [("total", "all", "人工总星文本", "sha-new", WEEK1, "manual")]  # 指纹重取


def test_fill_repeat_idempotent(conn):
    """重复回填幂等：同一批连续提交两次，第二次全部 skipped，库内文本与 source 不变。"""
    _add_repo(conn, "a/one")
    items = [_week_item("a/one", "周文本"), {"repo": "a/one", "kind": "summary", "period_label": "all", "text": "概要文本"}]
    first = _run_fill(conn, items, github=FakeGitHubClient(readmes={"a/one": ("readme", "sha-1")}))
    before = _rec_rows(conn, 1)
    second = _run_fill(conn, items, github=FakeGitHubClient(readmes={"a/one": ("readme", "sha-1")}))

    assert first["written"] == 2 and second == {"written": 0, "skipped": 2, "failed": 0, "errors": []}
    assert _rec_rows(conn, 1) == before


def test_fill_batch_duplicate_key_second_skipped(conn):
    """批内重复键（repo+kind+period_label）：按序处理，后到者因"已存在"计入 skipped。"""
    _add_repo(conn, "a/one")
    result = _run_fill(conn, [_week_item("a/one", "第一次"), _week_item("a/one", "第二次")])

    assert result["written"] == 1 and result["skipped"] == 1
    assert _rec_rows(conn, 1) == [("week", WEEK1, "第一次", None, WEEK1, "manual")]


def test_fill_rejects_expired_period_and_keeps_processing_batch(conn):
    """期次过期整条拒绝（errors 指向重新导出），同批其余条目照常写入。"""
    _add_repo(conn, "a/one")
    _add_repo(conn, "a/two")
    result = _run_fill(
        conn,
        [
            _week_item("a/one", "上周文本", period_label="2026-W31"),
            {"repo": "a/two", "kind": "quarter", "period_label": "2026-Q2", "text": "旧季文本"},
            _week_item("a/two", "本周文本"),
        ],
    )

    assert result["written"] == 1 and result["failed"] == 2
    assert [e["repo"] for e in result["errors"]] == ["a/one", "a/two"]
    assert "过期" in result["errors"][0]["reason"] and WEEK1 in result["errors"][0]["reason"]
    assert "过期" in result["errors"][1]["reason"] and QUARTER1 in result["errors"][1]["reason"]
    assert _rec_rows(conn, 1) == []
    assert _rec_rows(conn, 2) == [("week", WEEK1, "本周文本", None, WEEK1, "manual")]


def test_fill_total_summary_require_all_label(conn):
    """total/summary 的 period_label 固定 'all'，其余值整条拒绝；translate 忽略该字段。"""
    _add_repo(conn, "a/one")
    result = _run_fill(
        conn,
        [
            {"repo": "a/one", "kind": "total", "period_label": WEEK1, "text": "x"},
            {"repo": "a/one", "kind": "summary", "period_label": None, "text": "y"},
            _translate_item("a/one", "译文本体", src=text_fingerprint("english text"), period_label="随便"),
        ],
    )

    assert result["written"] == 1 and result["failed"] == 2
    assert all("固定为 all" in e["reason"] for e in result["errors"])
    assert conn.execute("SELECT description_zh FROM repos WHERE id = 1").fetchone()[0] == "译文本体"


def test_fill_rejects_unknown_repo_kind_and_text(conn):
    """逐条校验：repo 不在库 / kind 非法 / text 空或超长 / 非对象条目 → errors，不阻断同批。"""
    _add_repo(conn, "a/one")
    long_text = "长" * (MAX_TEXT_CHARS + 1)
    result = _run_fill(
        conn,
        [
            _week_item("ghost/none", "文本"),
            {"repo": "a/one", "kind": "month", "period_label": WEEK1, "text": "文本"},
            {"repo": "a/one", "kind": "week", "period_label": WEEK1, "text": "   "},
            {"repo": "a/one", "kind": "week", "period_label": WEEK1, "text": "推荐理由："},
            _week_item("a/one", long_text),
            {"repo": "a/one", "kind": "week", "period_label": WEEK1, "text": 123},
            "不是对象",
            _week_item("a/one", "正文"),
        ],
    )

    assert result["written"] == 1 and result["failed"] == 7
    reasons = " | ".join(e["reason"] for e in result["errors"])
    assert "不在跟踪池" in reasons and "kind 应为" in reasons and "清洗后为空" in reasons
    assert f"超过 {MAX_TEXT_CHARS} 字符上限" in reasons and "不是字符串" in reasons
    assert result["errors"][0]["repo"] == "ghost/none" and result["errors"][0]["kind"] == "week"


def test_fill_strips_prefix_and_quotes_before_write(conn):
    """写库前最小清洗（规格 Scenario）：带"推荐理由："前缀与引号包裹的文本只去壳，正文入库。"""
    _add_repo(conn, "a/one")
    result = _run_fill(
        conn,
        [
            _week_item("a/one", "推荐理由：“这是一个数据管道工具。”"),
        ],
    )

    assert result["written"] == 1
    assert _rec_rows(conn, 1)[0][2] == "这是一个数据管道工具。"


def test_fill_strips_ai_summary_prefix(conn):
    """清洗补「AI 概要：」前缀（评审 F3-5）：概要 prompt 钉死"不要加 AI 概要：前缀"，
    提交带该前缀的 summary 文本只去壳入库；中英文冒号两式都收。"""
    _add_repo(conn, "a/one")
    _add_repo(conn, "a/two")
    result = _run_fill(
        conn,
        [
            {"repo": "a/one", "kind": "summary", "period_label": "all", "text": "AI 概要：这是一个数据管道工具。"},
            {"repo": "a/two", "kind": "summary", "period_label": "all", "text": "AI 概要: 另一段概要正文。"},
        ],
    )

    assert result == {"written": 2, "skipped": 0, "failed": 0, "errors": []}
    rows = conn.execute(
        "SELECT r.full_name, c.text FROM recommendations c JOIN repos r ON r.id = c.repo_id ORDER BY r.full_name"
    ).fetchall()
    assert [(r["full_name"], r["text"]) for r in rows] == [
        ("a/one", "这是一个数据管道工具。"),
        ("a/two", "另一段概要正文。"),
    ]


# ---------- translate 原文指纹（src）：过期译文拒收 ----------


def test_export_translate_task_carries_src_fingerprint(conn):
    """① 导出的 translate 任务带 src（= 当时 description_en 的 sha1[:8]），其余四类任务不带 src 字段；
    作业单格式要求写明"translate 条目必须原样回带 src"，任务块把 src 值展示出来（本地工具有东西可抄）。"""
    _add_repo(conn, "a/one", quarter=True)
    payload = _run_export(conn, github=FakeGitHubClient())

    translate = _item(payload, "translate", "a/one")
    assert translate["src"] == hashlib.sha1("english text".encode("utf-8")).hexdigest()[:8]  # 8 位十六进制
    assert translate["src"] == text_fingerprint("english text")
    assert [item["kind"] for item in payload["items"] if "src" in item] == ["translate"]  # 其余四类不带
    sheet = render_export_text(payload)
    assert "translate 条目必须原样回带 src" in sheet
    assert f"回填字段：repo=a/one｜kind=translate｜period_label=null｜src={translate['src']}" in sheet
    assert sheet.count("｜src=") == 1  # 仅 translate 任务块展示 src（其余四类任务块没有该字段）


def test_fill_translate_with_matching_src_writes(conn):
    """② src 与当前原文一致 → 写入成功；同批 recommendations 类条目照常标 source='manual'（大写 src 也认）。"""
    _add_repo(conn, "a/one", description_en="english text")
    _add_repo(conn, "a/two", description_en="other text")
    result = _run_fill(
        conn,
        [
            _translate_item("a/one", "译文正文", src=text_fingerprint("english text")),
            _translate_item("a/two", "大写指纹", src=text_fingerprint("other text").upper()),
            _week_item("a/one", "周文本"),
        ],
    )

    assert result == {"written": 3, "skipped": 0, "failed": 0, "errors": []}
    zh = dict(conn.execute("SELECT full_name, description_zh FROM repos").fetchall())
    assert zh == {"a/one": "译文正文", "a/two": "大写指纹"}
    # 译文落 repos.description_zh（该表无 source 列）；recommendations 类条目才带 source 标记
    assert _rec_rows(conn, 1) == [("week", WEEK1, "周文本", None, WEEK1, "manual")]


def test_fill_translate_rejects_stale_src_after_description_change(conn):
    """③ 回填前原文已变（导出后 description_en 被改写）：src 不符 → 该条 errors、不写库；
    同批其他条目（src 与当前原文一致者）照常处理。"""
    _add_repo(conn, "a/one", description_en="english text")
    _add_repo(conn, "a/two", description_en="other text")
    stale = text_fingerprint("english text")
    conn.execute("UPDATE repos SET description_en = 'changed text' WHERE full_name = 'a/one'")
    conn.commit()

    result = _run_fill(
        conn,
        [
            _translate_item("a/one", "过期译文", src=stale),
            _translate_item("a/two", "正常译文", src=text_fingerprint("other text")),
        ],
    )

    assert result["written"] == 1 and result["failed"] == 1
    error = result["errors"][0]
    assert error["repo"] == "a/one" and error["kind"] == "translate"
    assert error["reason"] == (
        f"原文已变更（导出指纹 {stale} ≠ 当前 {text_fingerprint('changed text')}），请重新导出该条"
    )
    zh = dict(conn.execute("SELECT full_name, description_zh FROM repos").fetchall())
    assert zh == {"a/one": None, "a/two": "正常译文"}  # 过期译文不落库


@pytest.mark.parametrize("bad_src", [None, "", "   ", "zzzzzzzz", "1a2b3c", "1a2b3c4d5e", 12345])
def test_fill_translate_rejects_missing_or_invalid_src(conn, bad_src):
    """④ src 缺失（None/空串/非字符串）或非法（非 8 位十六进制）→ errors、不写库。"""
    _add_repo(conn, "a/one", description_en="english text")
    item = {"repo": "a/one", "kind": "translate", "period_label": None, "text": "译文正文"}
    if bad_src is not None:
        item["src"] = bad_src
    result = _run_fill(conn, [item])

    assert result["written"] == 0 and result["failed"] == 1 and result["skipped"] == 0
    assert conn.execute("SELECT description_zh FROM repos WHERE full_name = 'a/one'").fetchone()[0] is None
    assert "src" in result["errors"][0]["reason"]


def test_fill_non_translate_ignores_src_field(conn):
    """⑤ 非 translate 条目带/不带 src 都不影响既有行为（一律忽略，非法值也不校验）。"""
    _add_repo(conn, "a/one")
    result = _run_fill(
        conn,
        [
            {**_week_item("a/one", "周文本"), "src": "zzzzzzzz"},  # 非法 src：非 translate 不校验
            {"repo": "a/one", "kind": "total", "period_label": "all", "text": "总星文本"},
        ],
        github=FakeGitHubClient(readmes={"a/one": ("readme", "sha-1")}),
    )

    assert result == {"written": 2, "skipped": 0, "failed": 0, "errors": []}
    assert _rec_rows(conn, 1) == [
        ("total", "all", "总星文本", "sha-1", WEEK1, "manual"),
        ("week", WEEK1, "周文本", None, WEEK1, "manual"),
    ]


def test_fill_translate_guard_and_overwrite(conn):
    """translate 既有护栏保持（与自动路径同款 SQL：description_zh IS NULL AND description_en 未变）：
    已译 / 原文为空 → skipped；未译且 src 匹配 → 写入；overwrite 时直接覆盖（用户显式动作）。"""
    _add_repo(conn, "a/done", description_en="english text", description_zh="已译")
    _add_repo(conn, "a/todo", description_en="english text")
    _add_repo(conn, "a/empty", description_en=None)
    items = [
        _translate_item("a/done", "不该写", src=text_fingerprint("english text")),
        _translate_item("a/todo", "译文正文", src=text_fingerprint("english text")),
        _translate_item("a/empty", "无原文也写不了", src=text_fingerprint("")),  # 空串指纹：仍被下方护栏拦成 skipped
    ]
    result = _run_fill(conn, items)

    assert result["written"] == 1 and result["skipped"] == 2
    zh = dict(conn.execute("SELECT full_name, description_zh FROM repos").fetchall())
    assert zh == {"a/done": "已译", "a/todo": "译文正文", "a/empty": None}
    # overwrite：显式覆盖已译行（用户动作，不静默丢弃）
    result2 = _run_fill(
        conn,
        [_translate_item("a/done", "重译正文", src=text_fingerprint("english text"))],
        overwrite=True,
    )
    assert result2["written"] == 1
    assert conn.execute("SELECT description_zh FROM repos WHERE full_name = 'a/done'").fetchone()[0] == "重译正文"


def test_fill_total_summary_refetch_sha_with_failure_fallback(conn):
    """total/summary：指纹取回填时刻重新拉取的 sha；拉取失败写 NULL（人工行不参与窗口日比对，
    该指纹仅在该行日后被手动"重新生成"重写为 ai 行时才参与比 sha——不存在"下次窗口日自愈"）。"""
    _add_repo(conn, "a/ok")
    _add_repo(conn, "a/fail")
    github = FakeGitHubClient(readmes={"a/ok": ("readme", "sha-new")}, fail_for={"a/fail"})
    result = _run_fill(
        conn,
        [
            {"repo": "a/ok", "kind": "total", "period_label": "all", "text": "总星文本"},
            {"repo": "a/ok", "kind": "summary", "period_label": "all", "text": "概要文本"},
            {"repo": "a/fail", "kind": "total", "period_label": "all", "text": "总星文本"},
        ],
        github=github,
    )

    assert result["written"] == 3 and result["errors"] == []
    assert _rec_rows(conn, 1) == [
        ("summary", "all", "概要文本", "sha-new", WEEK1, "manual"),
        ("total", "all", "总星文本", "sha-new", WEEK1, "manual"),
    ]
    assert _rec_rows(conn, 2) == [("total", "all", "总星文本", None, WEEK1, "manual")]
    assert github.readme_calls.count("a/ok") == 1  # 同批同仓复用 _ReadmeState 缓存：只拉一次


def test_fill_quarter_row_written_with_current_labels(conn):
    """quarter 行：period_label=当季标签、generated_week=当周、readme_sha=NULL（与自动路径同口径）。"""
    _add_repo(conn, "a/one")
    result = _run_fill(conn, [{"repo": "a/one", "kind": "quarter", "period_label": QUARTER1, "text": "季榜文本"}])

    assert result["written"] == 1
    assert _rec_rows(conn, 1) == [("quarter", QUARTER1, "季榜文本", None, WEEK1, "manual")]


def test_fill_manual_row_protected_from_daily_ensure_window(conn):
    """端到端衔接：回填写入的 manual 行在窗口日不被每日 ensure 重生（跨模块口径一致），
    同仓缺失的新周行照常生成（source='ai'）。"""
    from app.ai import recommend_missing

    _add_repo(conn, "a/one", quarter=True)
    closed = asyncio.run(
        apply_fill(
            conn,
            items=[
                {"repo": "a/one", "kind": "quarter", "period_label": QUARTER1, "text": "人工季榜文本"},
                {"repo": "a/one", "kind": "total", "period_label": "all", "text": "人工总星文本"},
            ],
            github_client=FakeGitHubClient(readmes={"a/one": ("v1", "sha-v1")}),
            now=AS_OF_DT,
        )
    )
    assert closed["written"] == 2

    class RecordingAi:
        def __init__(self):
            self.recommend_dims: list[str] = []

        async def recommend(self, *, dimension, **kwargs):
            self.recommend_dims.append(dimension)
            return f"推荐语-{dimension}"

        async def summarize(self, **kwargs):
            return "概要文本"

    fake = RecordingAi()
    github = FakeGitHubClient(readmes={"a/one": ("v2", "sha-v2")})
    stats = asyncio.run(recommend_missing(conn, fake, now=WINDOW_DAY, github_client=github))

    assert "quarter" not in fake.recommend_dims and "total" not in fake.recommend_dims  # 人工行不触发 AI
    assert fake.recommend_dims == ["week"]  # 缺失的新周行（W33）照常生成
    assert stats["recommended"] == 1
    rows = _rec_rows(conn, 1)
    assert ("quarter", QUARTER1, "人工季榜文本", None, WEEK1, "manual") in rows
    assert ("total", "all", "人工总星文本", "sha-v1", WEEK1, "manual") in rows  # 指纹未被改写
    assert ("week", WEEK2, "推荐语-week", None, WEEK2, "ai") in rows


# ---------- 接口层（TestClient：真实 app；GitHub/AI client 走依赖注入替换） ----------


class FakeAiClient:
    """假 DeepSeek client（仅单个重生端点用例使用）：recommend 回显文本。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def recommend(self, *, full_name, description, language, categories, dimension, delta=None, stars=None, readme=None, pool_days=None):
        self.calls.append({"full_name": full_name, "dimension": dimension, "readme": readme})
        return f"推荐语-{full_name}-{dimension}"

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clear_overrides():
    """任何用例后清依赖注入残留（多用例共进程，防串）。"""
    yield
    app.dependency_overrides.clear()


def _make_client(tmp_path, monkeypatch, *, seed=None, override_ai=None, override_gh=None):
    db = tmp_path / "web.db"
    init_db(db)
    if seed is not None:
        c = get_conn(db)
        seed(c)
        c.close()
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    if override_ai is not None:
        app.dependency_overrides[_ai_client] = override_ai
    if override_gh is not None:
        app.dependency_overrides[_github_client] = override_gh
    return TestClient(app)


def _seed_web_repo(conn, name="a/one", *, description_en="english text", now=None):
    """接口用例造数：快照相对真实今天（周榜恒出席、total 榜恒出席），断言不随运行日期漂移。"""
    now = now or datetime.now(timezone.utc)
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, language, topics, dead, source, created_at)"
        " VALUES (?, ?, ?, 'Python', '[]', 0, 'test', ?)",
        (name, f"node-{name}", description_en, _iso(now - timedelta(days=8))),
    )
    repo_id = cur.lastrowid
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
        (repo_id, _iso(now - timedelta(days=7)), 900),
    )
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)", (repo_id, _iso(now), 1000)
    )
    conn.commit()
    return repo_id


def test_api_tasks_text_and_json_same_task_set(tmp_path, monkeypatch):
    """GET /api/local-ai/tasks：format=json 出结构化清单（六个顶层字段）；format=text 出可粘贴作业单
    （含输出格式要求与逐条 prompt），两形态任务集合一致；不依赖 AI client（无 DEEPSEEK key 也照常）。"""
    github = FakeGitHubClient(readmes={"a/one": ("# One", "sha-1")})
    with _make_client(tmp_path, monkeypatch, seed=_seed_web_repo, override_gh=lambda: github) as client:
        resp_json = client.get("/api/local-ai/tasks", params={"format": "json", "limit": 50})
        resp_text = client.get("/api/local-ai/tasks", params={"format": "text", "limit": 50})

    assert resp_json.status_code == 200
    payload = resp_json.json()
    assert {
        "as_of",
        "week_label",
        "quarter_label",
        "window_open",
        "remaining",
        "probe_truncated",
        "probe_skipped",
        "items",
    } <= set(payload)
    assert payload["week_label"] == _current_week_label()
    assert [item["kind"] for item in payload["items"]] == ["translate", "week", "quarter", "total", "summary"]
    assert [item["task_id"] for item in payload["items"]] == ["T1", "T2", "T3", "T4", "T5"]

    assert resp_text.status_code == 200
    assert resp_text.headers["content-type"].startswith("text/plain")
    body = resp_text.text
    assert "【输出格式要求】" in body and '"repo"' in body and '"period_label"' in body
    assert f"任务条数：{len(payload['items'])}" in body
    for index, item in enumerate(payload["items"], 1):
        assert f"【任务 {index}/{len(payload['items'])}】" in body
        assert item["system"] in body and item["user"] in body


def test_api_tasks_validation_and_limit_clamp(tmp_path, monkeypatch):
    """参数校验：kind/format 非法 → 400；limit 越界夹取（不报错、不返回无限量任务）。"""
    github = FakeGitHubClient()
    with _make_client(tmp_path, monkeypatch, seed=_seed_web_repo, override_gh=lambda: github) as client:
        assert client.get("/api/local-ai/tasks", params={"kind": "month"}).status_code == 400
        assert client.get("/api/local-ai/tasks", params={"format": "yaml"}).status_code == 400
        assert client.get("/api/local-ai/tasks", params={"kind": "week"}).status_code == 200
        big = client.get("/api/local-ai/tasks", params={"limit": 999, "format": "json"})
        assert big.status_code == 200 and len(big.json()["items"]) <= 50
        small = client.get("/api/local-ai/tasks", params={"limit": 0, "format": "json"})
        assert small.status_code == 200 and len(small.json()["items"]) == 1  # 下限 1
        assert client.get("/api/local-ai/tasks", params={"limit": "abc"}).status_code == 422  # 非整数：FastAPI 拦截


def test_api_tasks_uses_kind_filter_and_remaining(tmp_path, monkeypatch):
    """kind 过滤 + remaining：只出该类任务，remaining 给出同类剩余待办数。"""
    def seed(conn):
        _seed_web_repo(conn, "a/one")
        _seed_web_repo(conn, "a/two")

    with _make_client(tmp_path, monkeypatch, seed=seed, override_gh=lambda: FakeGitHubClient()) as client:
        resp = client.get("/api/local-ai/tasks", params={"kind": "translate", "limit": 1, "format": "json"})
    payload = resp.json()
    assert [item["kind"] for item in payload["items"]] == ["translate"]
    assert payload["remaining"] == 1  # 两个待译仓，本次导出 1 条


def test_api_fill_writes_and_renders_on_page(tmp_path, monkeypatch):
    """POST /api/local-ai/fill：成功 200 + 计数；回填的总星文本按 (dimension, period_label) 在总星页可见
    （页面模板零改动——沿用 {{ r.reason }} 取文本）。"""
    github = FakeGitHubClient(readmes={"a/one": ("# One", "sha-1")})
    with _make_client(tmp_path, monkeypatch, seed=_seed_web_repo, override_gh=lambda: github) as client:
        resp = client.post(
            "/api/local-ai/fill",
            json={
                "items": [
                    {"repo": "a/one", "kind": "total", "period_label": "all", "text": "推荐理由：这是总星推荐语。"},
                    {"repo": "a/one", "kind": "week", "period_label": _current_week_label(), "text": "本周周榜文本"},
                    {"repo": "a/one", "kind": "week", "period_label": "2026-W01", "text": "过期文本"},
                ]
            },
        )
        assert resp.status_code == 200
        assert resp.json()["written"] == 2 and resp.json()["failed"] == 1
        assert "过期" in resp.json()["errors"][0]["reason"]
        page = client.get("/total?board=all")
    assert page.status_code == 200
    assert "这是总星推荐语。" in page.text
    assert "推荐理由：这是总星推荐语。" not in page.text  # 前缀已清洗


def test_api_fill_bare_array_and_overwrite(tmp_path, monkeypatch):
    """裸数组等价 overwrite=false；overwrite=true 覆盖既有行并标记 manual。"""
    def seed(conn):
        repo_id = _seed_web_repo(conn)
        _insert_row(conn, repo_id, "total", "all", "旧文本", generated_week=_current_week_label())

    github = FakeGitHubClient(readmes={"a/one": ("# One", "sha-1")})
    with _make_client(tmp_path, monkeypatch, seed=seed, override_gh=lambda: github) as client:
        first = client.post(
            "/api/local-ai/fill",
            content=json.dumps([{"repo": "a/one", "kind": "total", "period_label": "all", "text": "裸数组文本"}]),
        )
        assert first.status_code == 200 and first.json() == {"written": 0, "skipped": 1, "failed": 0, "errors": []}
        second = client.post(
            "/api/local-ai/fill",
            json={"items": [{"repo": "a/one", "kind": "total", "period_label": "all", "text": "覆盖文本"}], "overwrite": True},
        )
    assert second.status_code == 200 and second.json()["written"] == 1

    conn = get_conn(tmp_path / "web.db")
    try:
        row = conn.execute("SELECT text, source FROM recommendations WHERE dimension = 'total'").fetchone()
        assert (row["text"], row["source"]) == ("覆盖文本", "manual")
    finally:
        conn.close()


def test_api_translate_export_then_fill_roundtrip_and_stale_rejection(tmp_path, monkeypatch):
    """端到端（HTTP 层）：导出 translate 任务 → 按 src 回填成功；原文在导出后被改写 → 旧 src 被拒、
    不写库，同批其他条目照常处理。"""
    def seed(conn):
        _seed_web_repo(conn, "a/one")
        _seed_web_repo(conn, "a/two")

    db = tmp_path / "web.db"
    with _make_client(tmp_path, monkeypatch, seed=seed, override_gh=lambda: FakeGitHubClient()) as client:
        items = client.get("/api/local-ai/tasks", params={"kind": "translate", "format": "json"}).json()["items"]
        by_repo = {item["repo"]: item for item in items}
        assert set(by_repo) == {"a/one", "a/two"} and all(len(item["src"]) == 8 for item in items)

        ok = client.post(
            "/api/local-ai/fill",
            json={
                "items": [
                    {"repo": "a/one", "kind": "translate", "period_label": None, "src": by_repo["a/one"]["src"], "text": "一的译文"}
                ]
            },
        )
        assert ok.status_code == 200 and ok.json()["written"] == 1

        conn = get_conn(db)  # 模拟导出后采集层改写原文（description_en 变化）
        conn.execute("UPDATE repos SET description_en = 'rewritten text' WHERE full_name = 'a/two'")
        conn.commit()
        conn.close()

        stale = client.post(
            "/api/local-ai/fill",
            json={
                "items": [
                    {"repo": "a/two", "kind": "translate", "period_label": None, "src": by_repo["a/two"]["src"], "text": "二的译文"},
                    {"repo": "a/two", "kind": "week", "period_label": _current_week_label(), "text": "周文本"},
                ]
            },
        )
        assert stale.status_code == 200
        body = stale.json()
        assert body["written"] == 1 and body["failed"] == 1
        assert "原文已变更" in body["errors"][0]["reason"] and by_repo["a/two"]["src"] in body["errors"][0]["reason"]

    conn = get_conn(db)
    try:
        zh = dict(conn.execute("SELECT full_name, description_zh FROM repos").fetchall())
        assert zh == {"a/one": "一的译文", "a/two": None}  # 过期译文不写库
        assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 1  # 同批 week 条目照常写入
    finally:
        conn.close()


def test_api_fill_limits(tmp_path, monkeypatch):
    """体量与批条目双上限：>1MB → 413（不写库）；>200 条 → 400；body 非法 → 400 带可操作文案。"""
    with _make_client(tmp_path, monkeypatch, seed=_seed_web_repo) as client:
        too_big = client.post("/api/local-ai/fill", content=b"x" * (1024 * 1024 + 1))
        assert too_big.status_code == 413 and "1MB" in too_big.json()["detail"]
        too_many = client.post(
            "/api/local-ai/fill",
            json={
                "items": [
                    {"repo": "a/one", "kind": "total", "period_label": "all", "text": f"文本{i}"} for i in range(201)
                ]
            },
        )
        assert too_many.status_code == 400 and "200" in too_many.json()["detail"]
        bad = client.post("/api/local-ai/fill", content="这不是 JSON")
        assert bad.status_code == 400 and "items" in bad.json()["detail"]
    conn = get_conn(tmp_path / "web.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 0  # 超限整批拒绝
    finally:
        conn.close()


def test_api_fill_needs_no_ai_client(tmp_path, monkeypatch):
    """AI_ENABLED=0（生产姿态）下回填照常可用：不新建、不依赖 AI client；空批返回全零。"""
    monkeypatch.setenv("AI_ENABLED", "0")
    with _make_client(tmp_path, monkeypatch, seed=_seed_web_repo, override_gh=lambda: FakeGitHubClient()) as client:
        assert client.get("/api/local-ai/tasks").status_code == 200
        resp = client.post("/api/local-ai/fill", json={"items": []})
    assert resp.status_code == 200 and resp.json() == {"written": 0, "skipped": 0, "failed": 0, "errors": []}


def test_api_single_recommend_overrides_manual_row_and_marks_ai(tmp_path, monkeypatch):
    """人工行对自动路径豁免，但不挡用户显式操作：单个"重新生成"覆盖 manual 行并标记 source='ai'（规格 Scenario）。"""
    def seed(conn):
        repo_id = _seed_web_repo(conn)
        _insert_row(conn, repo_id, "total", "all", "人工总星文本", source="manual")

    fake = FakeAiClient()
    github = FakeGitHubClient(readmes={"a/one": ("# One", "sha-new")})
    with _make_client(tmp_path, monkeypatch, seed=seed, override_ai=lambda: fake, override_gh=lambda: github) as client:
        resp = client.post("/api/recommend", json={"full_name": "a/one", "dimension": "total", "period_label": "all"})
    assert resp.status_code == 200
    conn = get_conn(tmp_path / "web.db")
    try:
        row = conn.execute("SELECT text, source, readme_sha FROM recommendations WHERE dimension = 'total'").fetchone()
        assert (row["text"], row["source"], row["readme_sha"]) == ("推荐语-a/one-total", "ai", "sha-new")
    finally:
        conn.close()
