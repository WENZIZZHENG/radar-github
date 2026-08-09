-- GitHub 雷达 SQLite schema（单文件 + WAL）。
-- 全部对象带 IF NOT EXISTS：init_db() 重复执行安全（启动接线归后续任务，接线后应用启动时无条件调用，不必维护迁移状态）。
-- 时间一律存 ISO 8601 文本：SQLite 无原生日期类型，ISO 文本可排序、可直接用 <= 比较。
-- 硬约定：所有写入方统一 UTC、定长 'YYYY-MM-DDTHH:MM:SSZ'——带时区偏移或缺秒的变体会让字典序比较静默错乱
--（检查点：T-003/T-005 采集入库处由代码保证）。

-- 仓库主表。中文翻译懒写入本表：首次需要展示时才翻译回填 description_zh，避免采集期批量调翻译 API。
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL UNIQUE,        -- owner/repo，天然唯一键，采集去重靠它
    description_en TEXT,                   -- GitHub 原文描述，官方允许为空
    description_zh TEXT,                   -- 中文翻译，懒写入：未翻译前保持 NULL
    language TEXT,                         -- 主语言，GitHub 上部分仓库无语言，允许 NULL
    topics TEXT NOT NULL DEFAULT '[]',     -- GitHub topics，JSON 数组字符串（S 档不建关系表）
    dead INTEGER NOT NULL DEFAULT 0,       -- 仓库删除/私有化标记：置 1 后停止采集，但保留历史快照
    source TEXT NOT NULL,                  -- 入池来源（trending / search 等），用于回溯数据质量
    created_at TEXT NOT NULL               -- 入池时间，ISO 8601
);
-- 分语言榜单按 language 过滤仓库后再算增量
CREATE INDEX IF NOT EXISTS idx_repos_language ON repos (language);

-- 星数快照：每日每仓库一行。榜单不做物化表，实时用两个截止日期各取"最近快照"求差值排序。
CREATE TABLE IF NOT EXISTS star_snapshots (
    repo_id INTEGER NOT NULL REFERENCES repos (id),
    captured_at TEXT NOT NULL,             -- 采集时间，ISO 8601
    stars INTEGER NOT NULL,
    PRIMARY KEY (repo_id, captured_at)
);
-- 主键 (repo_id, captured_at) 即榜单核心索引：
-- "每个仓库在某日之前的最近快照"按仓库逐个做索引定位（MAX(captured_at) <= 截止日），不走日期全表扫描，
-- 因此不再额外建 captured_at 单列索引。
-- 硬约束（T-007 榜单 SQL 必须遵守）：端点查询必须从 repos 驱动、每仓库 ORDER BY captured_at DESC LIMIT 1 走索引 seek；
-- 禁止对 star_snapshots 整体 GROUP BY 或相关子查询形态——实测 EXPLAIN QUERY PLAN 两种写法均为 2300 万行级全扫。

-- 关注：一个仓库至多一条记录，取关即删行
CREATE TABLE IF NOT EXISTS follows (
    repo_id INTEGER PRIMARY KEY REFERENCES repos (id),
    created_at TEXT NOT NULL
);

-- 自定义标签：同一仓库同一标签只存一次
CREATE TABLE IF NOT EXISTS tags (
    repo_id INTEGER NOT NULL REFERENCES repos (id),
    tag TEXT NOT NULL,
    PRIMARY KEY (repo_id, tag)
);
-- 主键服务"仓库 → 标签"方向；分主题榜单走"标签 → 仓库"反查，需独立索引
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags (tag);

-- 推荐理由：按周重新生成，同一仓库同一周只保留最新一条（重新生成用 INSERT OR REPLACE 覆盖）
CREATE TABLE IF NOT EXISTS recommendations (
    repo_id INTEGER NOT NULL REFERENCES repos (id),
    report_week TEXT NOT NULL,             -- ISO 周，格式如 2026-W32
    text TEXT NOT NULL,
    PRIMARY KEY (repo_id, report_week)
);
-- 周报展示按周取全量推荐理由
CREATE INDEX IF NOT EXISTS idx_recommendations_week ON recommendations (report_week);
