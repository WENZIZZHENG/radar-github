# 提案：榜单"新项目区"

## Why

周/季/总三榜目前只有主榜（Top 50）+ 新崛起区（入池不足一个统计窗口的缺席仓，Top 10）。实测生产数据：热门分类周榜 50 个坑位中巨头仓（总星 ≥5 万的老项目）常驻占 10%~36%（python 榜 17 个、frontend 17、typescript 16、other 18），用户无法"纯看新项目"。总榜更是被老巨头永久霸榜，是三张榜中信息量最低的。

目标：给用户一个"过滤后纯看新项目"的固定入口——每张分类榜增加"新项目区"（GitHub 创建 < 1 年），三榜统一。

## What Changes

- **数据层**：`repos` 表新增 `github_created_at` 列（GitHub 创建时间；现有 `created_at` 为入池时间，语义不动）。每日 GraphQL 批量采集落库时顺手写入（`NODES_QUERY` 已返回 `createdAt`，当前被丢弃——顺手回填，零额外 API 开销，跑一次日常采集全池回填完毕）；新仓入池路径（Search 发现 `ingest_items`、关注入池 `ingest_followed_repo`）同样顺手存储。
- **榜单计算**（`app/report.py` `compute_boards`）：三口径统一为每榜三段结构 主榜 / 新项目区 / 新崛起区：
  - 周榜新项目区：创建 < 1 年 + 出席，按周增量降序 Top 20；
  - 季榜新项目区：创建 < 1 年 + 出席，按季增量降序 Top 20；
  - 总榜新项目区：创建 < 1 年（无出席概念），按总星降序 Top 20；
  - 互斥规则：入池 < 5 天（周）/ < 86 天（季）在新崛起区，满最小窗口毕业进新项目区，满 1 年或凭实力进主榜，同仓永不重影；不足 20 行按实际列，不补位。
- **展示**：新项目区复用新崛起区组件样式（既有冻结视觉体系内复用）；榜单行全名旁加创建年份小灰字标注（如 "2024"）；每张分类榜的主榜行列表前补与两新区对仗的 h4 区头（周/季"主榜 · 按本期增星排序"、总榜"主榜 · 按总星排序"）——三区语法统一，同时解决新崛起区末行与主榜第 1 行视觉粘连（新崛起区有区头、主榜无，行组件同款无分隔）。
- **主榜口径不动**：纯增量功能，不踢任何老项目。
- **已知代价**：上线第一天新项目区为空或零星几行，次日日常采集回填后满血——顺手回填方案的既定代价，已拍板接受。

## Capabilities

### New Capabilities

- `new-project-zone`: 榜单新项目区的准入、排序、行数上限、三区互斥口径，及创建年份标注的展示行为。

### Modified Capabilities

（无——仓库 openspec/specs/ 下无既有 spec 覆盖榜单口径，本变更为全新能力。）

## Impact

- **代码**：`app/schema.sql`（repos 加列）、`app/collector/github.py`（GraphQL 查询已含 createdAt，无需改查询）、`app/collector/discover.py`（`_apply_snapshot_batch` 落库写 github_created_at）、`app/collector/snapshot.py`（`ingest_items`/`ingest_followed_repo` 插入带 github_created_at）、`app/report.py`（`compute_boards` 三段装配、Board/RisingRow 结构扩展、board_cache payload 白名单）、`app/web/`（模板：新项目区渲染 + 年份标注）。
- **数据**：生产库需迁移加列（`ALTER TABLE repos ADD COLUMN github_created_at TEXT`）；回填靠次日日常采集自动完成。
- **测试**：榜单计算、采集落库、缓存序列化的既有测试需适配新结构；新增新项目区口径测试。
- **外部依赖**：无新增；GitHub GraphQL/REST 字段均已在现有响应中。
- **项目 SOP**：核心链路（榜单计算）改动 → A 级预演；文字层《交互流程说明》先行确认；视觉为既有组件复用，免示意图档。
