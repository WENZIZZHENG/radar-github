# 规格：榜单新项目区（new-project-zone）

## Purpose

为 week/quarter/total 三口径的每张分类榜提供"新项目区"（创建未满 1 年的仓），与主榜、新崛起区构成三段结构；并为榜单行提供创建年份标注。本规格由 change `add-new-project-zone` 归档沉淀（2026-08-19）。

## Requirements

### Requirement: repos 表存储 GitHub 创建时间
系统 SHALL 在 `repos` 表新增 `github_created_at` 列（TEXT，NULL 允许，ISO 8601 UTC 定长格式），语义为 GitHub 上的仓库创建时间；既有 `created_at` 列（入池时间）语义 MUST NOT 改变。

#### Scenario: 每日 GraphQL 批量采集顺手回填
- **WHEN** 每日采集 `_apply_snapshot_batch` 处理 nodes(ids:) 返回的仓库节点
- **THEN** 系统将节点 `createdAt` 写入对应 `repos.github_created_at`（全池随一次日常采集回填完毕，无额外 API 请求）

#### Scenario: 新仓经 Search 发现入池
- **WHEN** `ingest_items` 插入新 repos 行
- **THEN** 系统将 Search 返回项的 `created_at` 一并存入 `github_created_at`；该字段缺失时存 NULL 且不中断采集

#### Scenario: 新仓经关注动态入池
- **WHEN** `ingest_followed_repo` 插入新 repos 行
- **THEN** 系统将 REST `/repos/{owner}/{repo}` 返回的 `created_at` 存入 `github_created_at`

### Requirement: 新项目区准入口径
系统 SHALL 为 week/quarter/total 三口径的每张分类榜计算"新项目区"。准入条件：
- week/quarter：仓库出席（`absent_reason is None`）且 `as_of − github_created_at < 365 天`；
- total：仓库 alive 且 `as_of − github_created_at < 365 天`（无出席概念）；
- 所有口径：`github_created_at IS NULL`（未回填）的仓 MUST NOT 进入新项目区；
- 所有口径：已进入本榜主榜 Top 50 的仓 MUST NOT 在新项目区重复出现（按 full_name 去重）。

#### Scenario: 新仓三区流转
- **WHEN** 一个创建未满 1 年的仓入池第 3 天（不满最小窗口）
- **THEN** 它只出现在新崛起区；入池满最小窗口且未凭增量进入主榜时只出现在新项目区；凭增量进入主榜 Top 50 时只出现在主榜——同一榜单内同仓不重影

#### Scenario: 未回填仓的处置
- **WHEN** 某仓 `github_created_at` 为 NULL
- **THEN** 它不进任何新项目区（主榜/新崛起区行为不受影响）

#### Scenario: 创建刚满 1 年
- **WHEN** 某仓 `as_of − github_created_at ≥ 365 天`
- **THEN** 它不进新项目区（主榜资格不受影响）

### Requirement: 新项目区排序与行数
新项目区 SHALL 按口径排序取 Top 20：week 按周增量降序（次序键 -stars、full_name，同主榜 `_top`）、quarter 按季增量降序（同）、total 按总星降序；不足 20 行 MUST 按实际数量展示，不补位。

#### Scenario: 热门分类超 20 仓
- **WHEN** 某榜满足准入的仓超过 20 个
- **THEN** 只展示排序前 20 行

#### Scenario: 冷门分类不足 20 仓
- **WHEN** 某榜满足准入的仓只有 3 个
- **THEN** 新项目区只展示 3 行，无凑数行

### Requirement: 榜单数据结构扩展
`Board` SHALL 新增 `fresh` 字段（新项目区行列表，缺省空列表）；主榜行、新项目区行、新崛起区行 SHALL 统一携带 `created_year: int | None`（`github_created_at` 的年份，NULL 时为 None）。board_cache 序列化白名单 MUST 同步包含 `fresh` 与 `created_year`，旧缓存缺键时 fail-loud 报错（部署后重跑预计算刷新）。

#### Scenario: 缓存缺新字段
- **WHEN** 反序列化一条本变更前写入的 board_cache 记录
- **THEN** 系统按白名单校验报错（fail-loud），不静默降级为 None

#### Scenario: 单榜整页模式（full_keys）
- **WHEN** 以 `full_keys` 收窄计算单榜
- **THEN** 仅指定榜计算 `fresh` 行，其余榜 `fresh` 为空列表；榜降级判定为"主榜＋新崛起区＋新项目区全空"

### Requirement: 页面展示
每张分类榜 SHALL 按"新崛起区、新项目区、主榜"的分段结构渲染（沿用 T-031 拍板的新区靠前顺序，新项目区紧跟新崛起区之后）；三区 SHALL 各有对仗的 h4 区头——新项目区（"新项目 · 创建未满 1 年"形态）、新崛起区（既有"新崛起 · …"不变）、主榜（周/季"主榜 · 按本期增星排序"、总榜"主榜 · 按总星排序"）；新项目区复用新崛起区组件样式；榜单行全名旁 SHALL 标注创建年份小灰字（`created_year` 为 None 时不渲染标注）；新项目区为空时 MUST NOT 渲染区头与空壳（主榜区头不受新区空满影响，恒渲染）。

#### Scenario: 三区各有区头不粘连
- **WHEN** 渲染一张三口径任一的分类榜
- **THEN** 新项目区、新崛起区、主榜各自带对仗 h4 区头，区与区之间视觉分隔明确（主榜不再是无区头行列表）

#### Scenario: 新区为空
- **WHEN** 某榜新项目区无任何行（如上线首日回填未完成）
- **THEN** 页面不渲染该区，主榜（含其区头）与新崛起区显示不受影响

#### Scenario: 年份标注
- **WHEN** 渲染一行 `created_year=2024` 的榜单行
- **THEN** 全名旁显示 "2024" 小灰字；`created_year=None` 的行不显示该标注

### Requirement: AI 覆盖范围纳入新项目区
系统 SHALL 把三口径榜的新项目区 Top 20 纳入 AI 范围集 S（翻译/推荐语/概要）：S 由"三口径榜（主榜 Top50 ∪ 新崛起区 Top10）去重 ∪ 关注集"扩为"三口径榜（主榜 Top50 ∪ 新崛起区 Top10 ∪ 新项目区 Top20）去重 ∪ 关注集"；S 集其余口径（S 外不译不生成、已译保留、每日自愈补齐等）MUST NOT 改变。

#### Scenario: 新项目区行有译文与推荐语
- **WHEN** 某仓仅因新项目区上榜（不在主榜/新崛起区/关注集）
- **THEN** 每日 AI ensure 为其补齐翻译/推荐语/概要，与主榜行同待遇
