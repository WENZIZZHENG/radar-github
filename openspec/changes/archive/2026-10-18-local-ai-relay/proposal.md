# 提案：本地 AI 导出/回填通道（local-ai-relay）

## Why

生产 AI 段已按本人要求停用（`AI_ENABLED=0`，2026-09-12 生效），但文本需求节奏不变——周榜每周全量重生约 830 条、每日自然新增约 120~190 条。本人手头有本地对话式 AI 工具（WorkBuddy 等，均不提供 API），可在空闲时批量生成。缺的只是一条"把待生成任务交出去、把结果收回来"的通道：判什么、生成什么、写到哪，全部沿用现有口径，只把"调用远端 AI"换成"导出给本地工具、回填结果"。

## What Changes

- **新增两个接口**（既有接口语义一律不动）：
  - `GET /api/local-ai/tasks`：按每日 ensure 的同一套判定（S 全集、缺失/窗口/README sha 口径）导出待生成任务，每条附完整 prompt（system + user，README 已按 `AI_README_HEAD_CHARS` 截断）与作业单格式说明；`format=text` 出可直接粘贴的作业单，`format=json` 出结构化清单。只读、无副作用、不占运行锁。
  - `POST /api/local-ai/fill`：接收本地工具产出（`repo` + `kind` + `period_label` + `text`），逐条校验后写入；默认只补缺失，`overwrite=true` 才覆盖；返回 written/skipped/errors 明细。
- **新增 `source` 列**（recommendations）：`ai`（默认，存量行与所有自动路径）/ `manual`（回填写入）；**自动路径不覆盖 `manual` 行**——窗口日的 quarter/total/summary 重生遇人工行直接跳过，缺失行照常补；周榜为每周期次新行，不受影响。
- **期次绑定**：导出与回填绑定同一期次（week/quarter 标签），跨期回填整条拒绝并提示重新导出。
- **回填文本最小清洗**：去首尾包裹引号与"推荐理由：/译文："类前缀（本地工具未必遵守 prompt 约束），清洗后仍为空的条目按格式错误拒绝。
- **不做**：不改每日跑批的判定与节奏（周榜维持每周重生）、不做页面面板（v1 纯接口）、不恢复 `AI_ENABLED`、不引入新依赖。

## Capabilities

### New Capabilities

- `local-ai-relay`: 本地 AI 导出/回填通道——待生成任务的导出判定（与每日 ensure 同口径）、作业单与结构化两种输出形态、回填的校验与写入语义（默认补缺、显式覆盖、期次校验、`source='manual'` 标记与文本清洗）、人工行对自动重生的豁免。

### Modified Capabilities

- `ai-refresh-cadence`: 半月窗口重生三处守卫（quarter/total/summary）各增加一条例外——`source='manual'` 的行不参与自动重生。

## Impact

- **代码**：`app/ai.py`（prompt 构造抽为纯函数、自动重生加人工行跳过守卫）、`app/schema.sql` + `app/db.py`（`source` 列与幂等迁移）、`app/local_ai.py`（新模块：导出判定与回填写入）、`app/web/routes.py`（两个新端点）、`tests/test_local_ai.py`（新）、`tests/test_ai.py`（人工行豁免用例）。
- **接口**：新增 2 个端点（一个只读、一个写入），公网入口仍是 Caddy Basic Auth 一层（复用，无新鉴权面）；导出 `limit` 与回填请求体各设上限。
- **数据**：`recommendations` 增加一列（默认 `ai`，存量行语义不变）；迁移幂等可重跑，回滚（旧代码）不受影响。
- **项目 SOP**：新功能 + 跨模块 + 新增外部接口 → 先 OpenSpec change（本提案）；《交互流程说明》需新增专章（本地生成回传流程）并回写 §8.4 写入口径；v1 无新增界面元素，免示意图。
