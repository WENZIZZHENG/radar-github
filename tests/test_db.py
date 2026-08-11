"""连接层与 schema 测试：一律用 tmp_path 真实文件——WAL 对 :memory: 库不生效，用内存库测了也是假绿。"""

import sqlite3

import pytest

from app.db import get_conn, init_db

EXPECTED_TABLES = {"repos", "star_snapshots", "follows", "tags", "recommendations"}
EXPECTED_INDEXES = {"idx_repos_language", "idx_tags_tag", "idx_recommendations_dim_period"}


def test_init_db_idempotent(tmp_path):
    db = tmp_path / "radar.db"
    init_db(db)
    conn = get_conn(db)
    conn.execute(
        "INSERT INTO repos (full_name, node_id, source, created_at) VALUES (?, ?, ?, ?)",
        ("octocat/keep", "node-keep", "search", "2026-08-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    init_db(db)  # 二次执行不抛异常；且已有数据必须保留——幂等语义是"跳过已存在"，不是 DROP 重建

    conn = get_conn(db)
    try:
        row = conn.execute("SELECT full_name FROM repos WHERE full_name = 'octocat/keep'").fetchone()
        assert row is not None
    finally:
        conn.close()


def test_wal_enabled(tmp_path):
    conn = get_conn(tmp_path / "radar.db")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()


def test_tables_and_indexes_exist(tmp_path):
    db = tmp_path / "radar.db"
    init_db(db)
    conn = get_conn(db)
    try:
        rows = conn.execute("SELECT type, name FROM sqlite_master").fetchall()
        tables = {row["name"] for row in rows if row["type"] == "table"}
        indexes = {row["name"] for row in rows if row["type"] == "index"}
        assert EXPECTED_TABLES <= tables
        assert EXPECTED_INDEXES <= indexes
    finally:
        conn.close()


def test_read_write_roundtrip(tmp_path):
    db = tmp_path / "radar.db"
    init_db(db)
    conn = get_conn(db)
    try:
        cur = conn.execute(
            "INSERT INTO repos (full_name, node_id, language, topics, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("octocat/hello", "node-hello", "Python", '["ai", "cli"]', "trending", "2026-08-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
            (cur.lastrowid, "2026-08-02T06:00:00Z", 1234),
        )
        conn.commit()
        row = conn.execute(
            "SELECT r.full_name, r.topics, s.stars FROM repos r JOIN star_snapshots s ON s.repo_id = r.id"
        ).fetchone()
        assert row["full_name"] == "octocat/hello"
        assert row["topics"] == '["ai", "cli"]'
        assert row["stars"] == 1234
    finally:
        conn.close()


def test_foreign_key_enforced(tmp_path):
    """外键约束必须真实生效：删掉 db.py 的 PRAGMA foreign_keys=ON，本用例必须变红——防 REFERENCES 静默失效。"""
    db = tmp_path / "radar.db"
    init_db(db)
    conn = get_conn(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, ?, ?)",
                (999, "2026-08-02T06:00:00Z", 1),
            )
    finally:
        conn.close()


# ---------- T-017：recommendations 旧结构 → 新结构幂等迁移 ----------


def _make_legacy_recommendations(db):
    """把新库的 recommendations 换成 T-011 旧结构（(repo_id, report_week, text)）并塞旧行。"""
    conn = get_conn(db)
    conn.execute("DROP TABLE recommendations")
    conn.execute(
        "CREATE TABLE recommendations ("
        "  repo_id INTEGER NOT NULL REFERENCES repos (id),"
        "  report_week TEXT NOT NULL,"
        "  text TEXT NOT NULL,"
        "  PRIMARY KEY (repo_id, report_week)"
        ")"
    )
    conn.execute(
        "INSERT INTO repos (full_name, node_id, source, created_at) VALUES (?, ?, ?, ?)",
        ("octocat/one", "node-one", "initial", "2026-07-01T00:00:00Z"),
    )
    rid = conn.execute("SELECT id FROM repos WHERE full_name = 'octocat/one'").fetchone()["id"]
    conn.execute(
        "INSERT INTO recommendations (repo_id, report_week, text) VALUES (?, ?, ?)",
        (rid, "2026-W32", "旧周推荐语"),
    )
    conn.commit()
    conn.close()
    return rid


def test_recommendations_migrated_from_legacy(tmp_path):
    """旧结构 → 新结构：旧行映射 dimension='week'、period_label=generated_week=report_week 搬入；
    readme_sha 保持 NULL；新索引就位。"""
    db = tmp_path / "legacy.db"
    init_db(db)
    rid = _make_legacy_recommendations(db)
    init_db(db)  # 触发迁移

    conn = get_conn(db)
    try:
        rows = conn.execute(
            "SELECT repo_id, dimension, period_label, text, readme_sha, generated_week FROM recommendations"
        ).fetchall()
        assert [
            (r["repo_id"], r["dimension"], r["period_label"], r["text"], r["readme_sha"], r["generated_week"])
            for r in rows
        ] == [(rid, "week", "2026-W32", "旧周推荐语", None, "2026-W32")]
        indexes = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "idx_recommendations_dim_period" in indexes
        assert "idx_recommendations_week" not in indexes  # 旧索引随旧表删除
    finally:
        conn.close()


def test_recommendations_migration_idempotent(tmp_path):
    """迁移后再次 init_db：不重复迁移、行数不变、新结构列仍完整（幂等语义）。"""
    db = tmp_path / "legacy.db"
    init_db(db)
    _make_legacy_recommendations(db)
    init_db(db)
    init_db(db)  # 第三次：已迁移结构再跑

    conn = get_conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 1
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(recommendations)")}
        assert {"repo_id", "dimension", "period_label", "text", "readme_sha", "generated_week"} <= cols
        row = conn.execute("SELECT dimension, period_label FROM recommendations").fetchone()
        assert (row["dimension"], row["period_label"]) == ("week", "2026-W32")  # 无重复行（未二次搬入）
    finally:
        conn.close()
