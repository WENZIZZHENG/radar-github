# 设计：榜单"新项目区"

## Context

现状（本变更不涉及的部分一句话带过）：榜单计算集中在 `app/report.py`，`compute_boards` 产出 17 张分类榜（语言 7 + 主题词表数 + 其他），每榜两段：主榜（出席仓按增量/总星 Top 50）+ 新崛起区（入池不足最小窗口的缺席仓按在池增量 Top 10）。结果经 `precompute_boards` 序列化进 `board_cache`（字段白名单手工展开），页面直读缓存。每日 GraphQL `nodes(ids:)` 批量采集已返回 `createdAt` 字段但落库时丢弃；`repos.created_at` 列存的是**入池时间**。

约束：S 档单人项目；核心链路改动走 A 级预演；fail-loud 为项目既定姿态；SQL 红线（端点查询走主键索引 seek）不得破。

## Goals / Non-Goals

**Goals:**
- 三口径（week/quarter/total）全部 17 张分类榜统一增加"新项目区"：GitHub 创建 < 1 年的仓，周/季按增量、总榜按总星，各 Top 20。
- 每榜三段互斥：新崛起区（缺席）/ 新项目区（出席且未上主榜）/ 主榜，同一仓库在同一张榜内只出现一次。
- 榜单行标注 GitHub 创建年份。
- 存量 6.4 万仓的创建时间随次日日常采集自动回填，零额外 API 开销。

**Non-Goals:**
- 主榜口径不变（不按年龄/星数踢任何老项目）。
- 新崛起区口径不变（仍按入池天数判定，不改为创建时间）。
- 不做"新项目区"独立页面/独立 tab。
- 不历史回填 board_cache（旧期次缓存没有新区数据属正常，不补）。

## Decisions

### 决策 1：新列 `repos.github_created_at`，不重用 `created_at`
`created_at` 语义是入池时间且已被"入池 N 天"标注等功能依赖，混入 GitHub 创建时间会同时脏两套语义。新列 `github_created_at TEXT NULL`（ISO 8601 UTC 定长硬约定同 schema 既有风格）。
备选：重用 `created_at` ——否决，理由如上。

### 决策 2：回填走每日 GraphQL 顺手写，零专项脚本
`NODES_QUERY` 已含 `createdAt`（github.py:55），每日 `_apply_snapshot_batch`（discover.py）落库时加一句 `UPDATE repos SET github_created_at = ? WHERE id = ?`（或并入既有 UPDATE）。跑一次日常采集全池回填完毕。新仓入池两路径（Search `ingest_items`、关注 `ingest_followed_repo`）INSERT 时带 `created_at` 字段（REST 响应本就返回）。
备选：专项一次性回填脚本——否决，每日采集白捡，专项脚本是重复建设。

### 决策 3："创建 < 1 年" = `as_of − github_created_at < 365 天`
常量 `_FRESH_MAX_AGE_DAYS = 365`。不按日历周年（2 月 29 日等边角），就用天数差，与项目"窗口按天数"的既有口径风格一致（`_INCREMENT_WINDOWS` 同为天数）。
`github_created_at IS NULL`（未回填）的仓**视为不满足**，不进新项目区——NULL 语义是"未知"，未知不冒充新项目。上线首日新区为空即此行为的直接表现（已拍板接受）。

### 决策 4：新项目区 = 出席 ∧ 创建<1年 ∧ 不在本榜主榜 Top 50 内，Top 20
- **出席**（`absent_reason is None`）才进——缺席仓归新崛起区，两区天然互斥；
- **排除本榜主榜已列仓**（按 full_name 去重）——兑现"凭实力进主榜则不在新区重影"的三段流水线语义；排序在主榜截断后做，先 `_top` 出主榜 50 行，再从剩余出席且 <1 年的仓里按同排序键取 Top 20；
- 排序键：week/quarter 按各自增量降序（次序键 `-stars, full_name` 同 `_top`）；total 按总星降序（无出席概念，准入仅 创建<1年 ∧ alive ∧ 不在主榜）；
- `_FRESH_ZONE_TOP_N = 20`；不足 20 按实际列，不补位（沿用新崛起区口径）。
备选：新项目区不排除主榜仓——否决，用户已拍板"同仓永不重影"。

### 决策 5：行对象加 `created_year: int | None`，Board 加 `fresh` 字段
- 新项目区行复用 `ReportRow`（stars/delta/window_days/captured_at 字段齐备，total 口径 delta=None 已支持），全榜行（主榜/新区/新崛起区）统一加 `created_year`——写入方从 `github_created_at` 取年份，None 时模板不渲染标注；
- `Board` dataclass 加 `fresh: list[ReportRow]`（默认空列表，放 `rising` 后）；board_cache payload 白名单手工展开处同步加 `fresh` 与 `created_year`——沿用 fail-loud 白名单风格，不一把梭；
- 模板：新项目区复用新崛起区组件样式，区头文案"新项目 · 创建未满 1 年"形态（文案在《交互流程说明》确认时定稿）；年份标注为全名旁小灰字（如 "· 2024"）；**主榜补对仗 h4 区头**（周/季"主榜 · 按本期增星排序"、总榜"主榜 · 按总星排序"）——三区语法统一，顺带解决既有粘连问题（新崛起区有区头、主榜无区头，两区行同款 `_row.html` 零分隔）；
- **AI 覆盖范围 S 集扩展**（ai.py）：三口径榜集由"主榜 Top50 ∪ 新崛起区 Top10"扩为"主榜 Top50 ∪ 新崛起区 Top10 ∪ 新项目区 Top20"——新项目区行的翻译/推荐语/概要与主榜行同待遇；S 集其余口径（关注集并入、S 外不译、自愈等）不变。

### 决策 6：T-026 单榜整页模式与新区的关系
`full_keys` 收窄时新项目区与主榜同榜同生死（指定榜的 fresh 才算，其余榜 fresh 为空列表、不计数）——新区行数小（≤20），不值得为它单独归桶计数；降级判定"主榜＋新区全空"改为"主榜＋新崛起区＋新项目区全空"。

## Risks / Trade-offs

- [上线首日新项目区为空（回填未完成）] → 已拍板接受；模板对空新区不渲染区头（与新崛起区空时行为一致），页面无残破态。
- [GitHub `createdAt` 对极老仓/迁移仓恒为 GitHub 记录的创建时间，无缺失风险]；REST Search 对个别仓 `created_at` 理论上必有（API 契约），但写入方仍按 `item.get("created_at")` 容错为 NULL——NULL 不进新区，不 fail-loud（采集主链路不因一个展示字段中断）。
- [新项目区与主榜去重依赖 full_name 比较] → 同库内 full_name 唯一（repos 唯一索引），去重安全。
- [board_cache 旧缓存缺 `fresh`/`created_year` 键] → 反序列化缺键按既有白名单风格直接报错 fail-loud，部署后**必须重跑预计算**刷新三口径缓存（部署步骤含此动作，与 T-032 部署后操作同款）。

## Migration Plan

1. `schema.sql` 加列 + 生产库 `ALTER TABLE repos ADD COLUMN github_created_at TEXT`（SQLite 加列瞬时不锁表）；
2. 代码部署（采集落库 + 榜单计算 + 模板）→ `systemctl restart radar`；
3. 手跑一轮预计算刷新 board_cache（页面立即可用，新区暂空）；
4. 次日 05:00 日常采集跑完自动回填全池 `github_created_at`；
5. 回填后重跑一次预计算（或等当日采集后自动预计算），新区满血。
回滚：代码回滚重启即可，`github_created_at` 列留库无害（无其他写入方）。

## Open Questions

- 新项目区区头最终文案、年份标注的精确视觉样式 → 《交互流程说明》文字层确认时定稿（本设计只钉行为口径）。
