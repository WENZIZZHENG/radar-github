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


def init_db(db_path: str | Path | None = None) -> None:
    """执行 schema.sql 建表；schema 全量 IF NOT EXISTS，重复执行安全。"""
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
