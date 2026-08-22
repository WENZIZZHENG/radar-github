# 提案：AI 推荐语/概要重生节奏降本（ai-refresh-cadence）

## Why

AI 调用成本实测过高：每日 ensure 跑批中，total 维度因 README blob sha 变化触发重生约 440~670 次/周，summary 维度同口径约 623~803 次/周，quarter 维度因 `generated_week ≠ 当周` 每周一重生约 96~124 次/周。单人使用场景下，推荐语/概要文本的"每日最新"收益远不足以覆盖每周 1500~2000 次调用的成本。经与本人逐问确认，将非周榜维度的重生节奏从"每日/每周"收紧为"半月窗口"。

## What Changes

- **总榜推荐语（dimension='total'）**：README 变更重生从"每日比对 sha 变了就重生"降为"仅在每月 1 号、15 号的跑批中比对重生"。非窗口日对既有行完全不拉 README、不比 sha、不重生；缺失行照常补缺生成（不受窗口限制）。
- **AI 概要（dimension='summary'）**：同 total 段，README 变更重生并入同一半月窗口；缺失行照常补缺生成。
- **季榜推荐语（dimension='quarter'）**："每周一重生"（`generated_week ≠ 当周` 即 REPLACE）并入同一节奏：仅在每月 1 号、15 号执行重生；缺失行照常补缺生成。
- **周榜维度（dimension='week'）**：维持现状，每周一全量重生（本人拍板不变）。
- **手动批量补缺 worker（refresh=False）**：行为不变，只补缺、不重生。
- **README 截断上限**：`.env.example` 中 `AI_README_HEAD_CHARS` 从注释态 8000 改为正式项 3000（降本后的现行口径）；`app/config.py` 代码默认值 8000 不动，由三处 env 显式配置兜底。

## Capabilities

### New Capabilities

- `ai-refresh-cadence`: AI 推荐语/概要的半月窗口重生策略——每月 1 号、15 号为刷新窗口，窗口内才比对 README sha 变更/季榜 generated_week 过期并触发 REPLACE；非窗口日对既有行零 GitHub API 调用；缺失行不受窗口限制照常补缺；周榜与手动批量补缺路径行为不变。

### Modified Capabilities

（无——`openspec/specs/` 尚无既有规格覆盖 AI 推荐语/概要刷新策略，本变更定义该策略。）

## Impact

- **代码**：`app/ai.py`（新增窗口判定 helper，调整 quarter/total/summary 三段重生守卫）、`.env.example`（`AI_README_HEAD_CHARS=3000`）、`tests/test_ai.py`（旧口径用例改半月窗口口径并新增覆盖用例）。
- **外部依赖**：无新增。
- **成本**：预计 total/summary/quarter 三维度周调用量从 1500~2000 次降至约原 1/7~1/15（取决于窗口日分布）。
- **数据**：无 schema 变更、无数据迁移；既有推荐语/概要行保留，非窗口日不删除/不覆盖。
- **项目 SOP**：改用户可见口径（推荐语文本刷新频率）与核心链路（每日 ensure）→ 先 OpenSpec change，A 级预演；无新视觉元素，免示意图。
