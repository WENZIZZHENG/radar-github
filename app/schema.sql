-- GitHub 雷达 SQLite schema（单文件 + WAL）。
-- 全部对象带 IF NOT EXISTS：init_db() 重复执行安全（启动接线归后续任务，接线后应用启动时无条件调用，不必维护迁移状态）。
-- 时间一律存 ISO 8601 文本：SQLite 无原生日期类型，ISO 文本可排序、可直接用 <= 比较。
-- 硬约定：所有写入方统一 UTC、定长 'YYYY-MM-DDTHH:MM:SSZ'——带时区偏移或缺秒的变体会让字典序比较静默错乱
--（检查点：T-003/T-005 采集入库处由代码保证）。

-- 仓库主表。中文翻译懒写入本表：首次需要展示时才翻译回填 description_zh，避免采集期批量调翻译 API。
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL UNIQUE,        -- owner/repo，天然唯一键，采集去重靠它
    node_id TEXT NOT NULL UNIQUE,          -- Relay 全局 ID：每日 GraphQL nodes(ids:) 批量采集的入口（T-006 勘误补列）
    description_en TEXT,                   -- GitHub 原文描述，官方允许为空
    description_zh TEXT,                   -- 中文翻译，懒写入：未翻译前保持 NULL
    language TEXT,                         -- 主语言，GitHub 上部分仓库无语言，允许 NULL
    topics TEXT NOT NULL DEFAULT '[]',     -- GitHub topics，JSON 数组字符串（S 档不建关系表）
    dead INTEGER NOT NULL DEFAULT 0,       -- 仓库删除/私有化标记：置 1 后停止采集，但保留历史快照
    source TEXT NOT NULL,                  -- 入池来源（trending / search 等），用于回溯数据质量
    created_at TEXT NOT NULL,              -- 入池时间，ISO 8601
    github_created_at TEXT                 -- GitHub 上的仓库创建时间（T-033，ISO 8601 定长）；语义区别于 created_at（入池时间），
                                           -- 每日 GraphQL 采集顺手回填；NULL = 未回填（新项目区准入按 NULL 一律排除）
);
-- 分语言榜单按 language 过滤仓库后再算增量
CREATE INDEX IF NOT EXISTS idx_repos_language ON repos (language);

-- 星数快照：每日每仓库一行。榜单端点快照源——预计算（T-027）与实时降级两条路径都从本表取数，
-- 快照只在每日采集时写入，两次采集之间库内不变，预计算等价性以此为依据。
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

-- 推荐理由（T-017 三口径分维度）＋ AI 概要（T-024）：同一仓库按"维度 × 期次标签"各存一条——周/季文本带增量语境
--（期次标签换行/REPLACE；周维度每周重生，季维度仅在半月窗口日 1 号/15 号 REPLACE）；
-- 总星文本存量语境（period_label 固定 'all'，懒生成＋半月窗口日 README 变更重生）；
-- 概要与推荐语并存（推荐语＝为什么值得关注，营销视角；概要＝是什么，README 文档视角、无维度概念，
-- 全页面同一条，period_label 恒 'all'，懒生成＋半月窗口日 README 变更重生，无任何手动入口——§11）。
-- readme_sha 存 README blob sha 指纹供每日 ensure 在半月窗口日比对（NULL＝未拉取过；NULL 仓后续出现 README 视为变更自愈）；
-- generated_week 写生成时所在 ISO 周（周文本"本周已生成"幂等与季文本窗口日 REPLACE 判断用）。
-- 旧结构 (repo_id, report_week, text) 与 3 值 CHECK 结构的幂等迁移在 db.py init_db（PRAGMA table_info 探列
-- / sqlite_master 读建表 SQL），本定义只服务新装库（最终结构 = 4 值 CHECK，两条迁移路径收敛于此）。
CREATE TABLE IF NOT EXISTS recommendations (
    repo_id INTEGER NOT NULL REFERENCES repos (id),
    dimension TEXT NOT NULL CHECK (dimension IN ('week', 'quarter', 'total', 'summary')),
    period_label TEXT NOT NULL,             -- 周标签（2026-W32）/ 季标签（2026-Q3）/ 总星与概要固定 'all'
    text TEXT NOT NULL,
    readme_sha TEXT,                        -- README blob sha：未拉取过保持 NULL
    generated_week TEXT NOT NULL,           -- 生成时所在 ISO 周，格式如 2026-W32
    PRIMARY KEY (repo_id, dimension, period_label)
);
-- 按维度×期次取全量推荐理由（周报页按周取、季页按季取、总星/关注页取 total/'all'）
CREATE INDEX IF NOT EXISTS idx_recommendations_dim_period ON recommendations (dimension, period_label);

-- 榜单预计算缓存（T-027）：每日采集后对三口径各 compute_boards 全量一次落表（JSON blob，S 档简单优先），
-- 页面打开直读；缺失/过期（采集写了新快照但预计算未跑/失败）时页面降级实时算——缓存只换"何时算、结果存哪"，
-- 计算口径仍走 app.report.compute_boards，实时路径不改。period 主键一行一口径，无需额外索引。
CREATE TABLE IF NOT EXISTS board_cache (
    period TEXT PRIMARY KEY,             -- 'week'/'quarter'/'total'
    label TEXT NOT NULL,                 -- 周/季期次标签（2026-W33/2026-Q3）；total 固定 'all'
    as_of TEXT NOT NULL,                 -- 预计算口径端点（UTC 定长 ISO，schema 硬约定）
    payload TEXT NOT NULL,               -- compute_boards 全量 17 榜的 JSON 序列化（字段白名单手工展开）
    computed_at TEXT NOT NULL            -- 落表时刻（UTC 定长 ISO）
);

-- 候选词扫描（T-032，§15）：每周一扫描池内 topics 词频，未命中词表且词频达阈值的词
-- 经 DeepSeek 出建议主题（或"不建议收录"）后全量覆盖式落库（DELETE 后 INSERT，只留最近一轮）；
-- 页面只读展示（P7 /topic-candidates），永不写 config/topics.yaml（决策 12，§15.4）。
-- scanned_at 为扫描时刻（UTC 定长 ISO，schema 硬约定），页面取前 10 位显示日期。
CREATE TABLE IF NOT EXISTS topic_candidates (
    term TEXT PRIMARY KEY,          -- 候选词（GitHub topics 原值，小写 kebab-case）
    suggested_topic TEXT NOT NULL,  -- AI 建议主题（词表主题 label）或"不建议收录"
    pool_count INTEGER NOT NULL,    -- 池内出现次数（dead=0 仓库 topics 含该词的仓库数）
    scanned_at TEXT NOT NULL        -- 最近扫描时刻（UTC 定长 ISO）
);
-- 最近一轮成功扫描记录（单行）：无候选时表空也能区分"扫描过但无候选"与"从未成功扫描"
-- 两种空态（§15.3：后者注明"扫描未运行过（AI 未配置）"）；每次成功扫描 REPLACE 固定主键 1
CREATE TABLE IF NOT EXISTS candidate_scans (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    scanned_at TEXT NOT NULL,       -- 成功扫描时刻（UTC 定长 ISO）
    term_count INTEGER NOT NULL     -- 本轮候选词数（0 = 扫描成功但无候选）
);
