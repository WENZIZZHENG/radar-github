"""连接层与 schema 测试：一律用 tmp_path 真实文件——WAL 对 :memory: 库不生效，用内存库测了也是假绿。"""

import sqlite3

import pytest

from app.db import get_conn, init_db

EXPECTED_TABLES = {"repos", "star_snapshots", "follows", "tags", "recommendations"}
EXPECTED_INDEXES = {"idx_repos_language", "idx_tags_tag", "idx_recommendations_week"}


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
