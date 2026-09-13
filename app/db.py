"""SQLite 连接层：单文件库 + WAL。

WAL 让读（榜单页面）与写（每日采集）互不阻塞，且模式一旦开启即持久记录在库文件中，
但仍逐连接显式设置，保证语义一目了然；外键在 SQLite 里默认关闭，必须逐连接开启，
否则 schema 中的 REFERENCES 约束形同虚设。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import get_settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开连接并开启 WAL / 外键；db_path 缺省取配置，测试传 tmp 路径覆盖。"""
    path = Path(db_path) if db_path is not None else get_settings().db_path
    # data/ 目录不入仓，首次运行必须补建，否则 sqlite3.connect 直接报 unable to open
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout 显式摆到明处：并发写冲突时 busy 等待 5 秒再报 database is locked（CPython 默认即 5，不依赖隐式行为）
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_recommendations(conn: sqlite3.Connection) -> None:
    """T-017 幂等迁移：recommendations 旧结构 (repo_id, report_week, text) → 新结构
    (repo_id, dimension, period_label, text, readme_sha, generated_week)。

    旧行映射 dimension='week'、period_label=generated_week=report_week 搬入（T-011 生成的按周推荐语
    即周维度行，语义无损；真实库现 0 行但迁移逻辑必须正确）；已迁移（有 dimension 列）/表不存在/
    未知结构（无 report_week）一律跳过。必须在 schema.sql 之前执行：新 schema 的
    CREATE INDEX IF NOT EXISTS idx_recommendations_dim_period 引用新列，旧表未换新会因列不存在报错。
    T-024：本迁移直接建 4 值 CHECK 最终结构（含 'summary'），与 _migrate_recommendations_summary
    收敛到同一结构——本迁移跑完后 summary 迁移检测含 'summary' 自然跳过。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(recommendations)")}
    if "dimension" in cols:
        return  # 已迁移（新装库直接新结构，或此前已迁移过）：幂等跳过
    if not cols or "report_week" not in cols:
        return  # 表不存在（新装库走 schema.sql）或未知结构：防御，宁可留旧表也不猜
    conn.execute("ALTER TABLE recommendations RENAME TO recommendations_legacy")
    conn.execute(
        """
        CREATE TABLE recommendations (
            repo_id INTEGER NOT NULL REFERENCES repos (id),
            dimension TEXT NOT NULL CHECK (dimension IN ('week', 'quarter', 'total', 'summary')),
            period_label TEXT NOT NULL,
            text TEXT NOT NULL,
            readme_sha TEXT,
            generated_week TEXT NOT NULL,
            PRIMARY KEY (repo_id, dimension, period_label)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)
        SELECT repo_id, 'week', report_week, text, NULL, report_week FROM recommendations_legacy
        """
    )
    conn.execute("DROP TABLE recommendations_legacy")  # 旧索引 idx_recommendations_week 随表一并删除
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recommendations_dim_period ON recommendations (dimension, period_label)"
    )


def _migrate_recommendations_summary(conn: sqlite3.Connection) -> None:
    """T-024 幂等迁移：3 值 CHECK 的 recommendations 表 → 4 值 CHECK（新增 'summary' 维度）。

    检测口径：读 sqlite_master 的建表 SQL，不含 'summary' → 重建表（RENAME 旧表 → 按最终结构
    CREATE → INSERT SELECT 全列搬数据 → DROP 旧表 → 补建维度×期次索引）；含则跳过。
    表不存在（新装库）跳过——schema.sql 直接建最终结构（4 值 CHECK）。
    必须与 _migrate_recommendations 一起在 schema.sql 之前执行：schema.sql 的 CREATE TABLE
    IF NOT EXISTS 不会改既有表的 CHECK 约束，只能靠重建表升级；顺序在其后（旧结构库先经
    T-017 迁移换新表，本迁移检测含 'summary' 自然跳过，两条路径收敛同一最终结构）。
    防御：sqlite_master 无建表 SQL（异常形态）不猜直接跳过。
    """
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'recommendations'"
    ).fetchone()
    if sql is None or sql[0] is None:
        return  # 表不存在（新装库走 schema.sql）或建表 SQL 缺失：防御
    if "'summary'" in sql[0]:
        return  # 已是 4 值 CHECK 最终结构：幂等跳过
    conn.execute("ALTER TABLE recommendations RENAME TO recommendations_legacy_summary")
    conn.execute(
        """
        CREATE TABLE recommendations (
            repo_id INTEGER NOT NULL REFERENCES repos (id),
            dimension TEXT NOT NULL CHECK (dimension IN ('week', 'quarter', 'total', 'summary')),
            period_label TEXT NOT NULL,
            text TEXT NOT NULL,
            readme_sha TEXT,
            generated_week TEXT NOT NULL,
            PRIMARY KEY (repo_id, dimension, period_label)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO recommendations (repo_id, dimension, period_label, text, readme_sha, generated_week)
        SELECT repo_id, dimension, period_label, text, readme_sha, generated_week
        FROM recommendations_legacy_summary
        """
    )
    conn.execute("DROP TABLE recommendations_legacy_summary")  # 旧索引随旧表一并删除
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recommendations_dim_period ON recommendations (dimension, period_label)"
    )


def _migrate_recommendations_source(conn: sqlite3.Connection) -> None:
    """local-ai-relay 幂等迁移：recommendations 补 source 列（'ai' 自动路径 / 'manual' 本地回填）。

    检测口径：PRAGMA table_info 已含 source → 跳过；表不存在（新装库走 schema.sql）→ 跳过；
    否则 ALTER TABLE ADD COLUMN（SQLite 的 O(1) 元数据操作，存量行按 DEFAULT 'ai' 补齐，语义不变）。
    必须排在 _migrate_recommendations_summary 之后：summary 迁移整表重建（新表结构不含本列），
    先加列会被那次重建丢掉。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(recommendations)")}
    if not cols or "source" in cols:
        return  # 表不存在或已有 source 列：幂等跳过
    conn.execute("ALTER TABLE recommendations ADD COLUMN source TEXT NOT NULL DEFAULT 'ai'")


def init_db(db_path: str | Path | None = None) -> None:
    """执行 schema.sql 建表；schema 全量 IF NOT EXISTS，重复执行安全。

    T-017：先跑 recommendations 幂等迁移（旧表换新结构），再 executescript——
    否则新 schema 的 CREATE INDEX 会撞上旧表缺列报错。
    T-024：顺序为 _migrate_recommendations → _migrate_recommendations_summary → executescript——
    summary 迁移把 3 值 CHECK 表升级为 4 值（schema.sql 的 CREATE TABLE IF NOT EXISTS 不改既有
    表约束，只能重建）；旧结构库先经 T-017 迁移直接建 4 值最终结构，summary 迁移检测跳过。
    local-ai-relay：source 列迁移排在这两条之后、executescript 之前——summary 迁移整表重建会丢掉
    先加的列，故必须等它跑完再补列。
    """
    conn = get_conn(db_path)
    try:
        _migrate_recommendations(conn)
        _migrate_recommendations_summary(conn)
        _migrate_recommendations_source(conn)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
