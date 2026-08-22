# 任务清单：榜单"新项目区"

> 流程口径（项目 SOP）：文字层先行——任务 1.1《交互流程说明》更新需本人确认后再派实施；核心链路改动，A 级预演；实施派 DeepSeek，评审派 k3。

## 1. 文字层与数据层

- [x] 1.1 更新《交互流程说明》：三榜三段结构（主榜/新项目区/新崛起区）、区头文案"新项目 · 创建未满 1 年"、年份标注样式说明 → 本人确认（2026-08-18 §16 v2.7 已确认）
- [x] 1.2 `app/schema.sql`：repos 表加 `github_created_at TEXT`（NULL 允许，ISO 8601 UTC 定长）；生产库执行 `ALTER TABLE repos ADD COLUMN github_created_at TEXT`
- [x] 1.3 `app/collector/discover.py`：`_apply_snapshot_batch` 落库时写入节点 `createdAt` → `github_created_at`（顺手回填）
- [x] 1.4 `app/collector/snapshot.py`：`ingest_items`（Search 入池）与 `ingest_followed_repo`（关注入池）INSERT 带 `github_created_at`（取 REST 返回 `created_at`，缺失存 NULL 不中断）

## 2. 榜单计算（app/report.py）

- [x] 2.1 常量 `_FRESH_MAX_AGE_DAYS = 365`、`_FRESH_ZONE_TOP_N = 20`；模块 docstring 口径段同步更新
- [x] 2.2 `ReportRow`/`RisingRow` 加 `created_year: int | None`；`Board` 加 `fresh: list[ReportRow]`（默认空列表，放 `rising` 后）
- [x] 2.3 `compute_boards`：三口径各榜计算新项目区——出席（total 豁免）∧ `as_of − github_created_at < 365 天` ∧ 不在本榜主榜 Top 50（full_name 去重），week/quarter 按增量、total 按总星降序 Top 20；NULL 一律不进
- [x] 2.4 `full_keys` 单榜模式适配：仅指定榜算 `fresh`；榜降级判定改为"主榜＋新崛起区＋新项目区全空"
- [x] 2.5 board_cache 序列化/反序列化白名单加 `fresh`、`created_year`（缺键 fail-loud 风格不变）

## 3. 页面展示（app/web/）

- [x] 3.1 模板：新项目区渲染（复用新崛起区组件样式，区头文案按 1.1 定稿），空区不渲染
- [x] 3.2 模板：榜单行全名旁创建年份小灰字标注（`created_year=None` 不渲染）
- [x] 3.3 模板：主榜补对仗 h4 区头（周/季"主榜 · 按本期增星排序"、总榜"主榜 · 按总星排序"），解决新崛起区与主榜粘连
- [x] 3.4 ai.py：S 集扩展——三口径榜集加入新项目区 Top20（翻译/推荐语/概要同主榜行待遇，其余 S 口径不变）

## 4. 测试

- [x] 4.1 新增：新项目区准入（出席/缺席/total/NULL/满 365 天边界）、与主榜去重不重影、Top 20 截断与不补位、三区流转互斥
- [x] 4.2 新增：采集落库三路径（_apply_snapshot_batch / ingest_items / ingest_followed_repo）写 `github_created_at`
- [x] 4.3 适配：既有测试（Board 结构、payload 白名单、模板断言）同步新字段；board_cache 缺键 fail-loud 用例更新
- [x] 4.4 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1` 一次跑绿

## 5. 评审与交付

- [x] 5.1 k3 独立评审，中/高 findings 清零（最多三轮）（零 F1/F2 一轮收口，F3×4 留痕）
- [x] 5.2 部署生产：迁移加列 → 部署重启 → 手跑预计算（新区暂空）→ 次日采集回填后重跑预计算（2026-08-19 完成：ALTER TABLE＋解包重启＋预计算 week 796/total 850；手动同步一轮 github_created_at 3232/3232 全量回填）
- [x] 5.3 A 级预演（真实环境，含回填后新区满血核对）+ 复验步骤卡 → 本人走查（预演全绿，产物 data/t033_t034_rehearse/；本人走查通过 2026-08-19）
- [x] 5.4 通过后当场归档（任务拆解表主表行+详情卡搬归档），文档按 §5 节奏提交
