# 提案：AI JSON 产出调用启用 response_format（ai-json-response-format）

## Why

`DeepSeekClient` 三个 JSON 产出方法（`suggest_topics` / `understand_intent` / `select_and_reason`）当前请求不带 `response_format`，纯靠 prompt 要求"只输出 JSON"，返回文本再由 `_parse_suggested_topics` 手搓容错解析（剥 markdown 围栏＋大括号截取）；模型一旦返回杂质就走各调用的降级路径（搜索精选退化为粗排直出无推荐理由、意图理解退化为单关键词等），降级语义正确但失败率与 token 浪费都偏高。生产实测（2026-10-12 本人服务器留痕）：commandcode 网关＋`deepseek/deepseek-v4.1-flash` 对 `response_format={"type":"json_object"}` 返回 HTTP 200、合法 JSON、耗时 3.0s（对比默认调用 22.7s/1454 reasoning tokens）——协议层约束既降解析失败率，又显著降时延与 reasoning token 消耗。本变更触及 AI 外部协议参数，按项目 §1 口径先建 OpenSpec change。

## What Changes

- **仅三个 JSON 产出方法的请求加 `response_format={"type": "json_object"}`**：`suggest_topics`（app/ai.py:407）、`understand_intent`（app/ai.py:429）、`select_and_reason`（app/ai.py:479）。实现经 `_chat` 单一出口透传（新增可选参数，缺省不传该字段，纯文本方法请求体逐字节不变）。
- **prompt 内容一字不改**；纯文本产出方法（translate / recommend / summarize）不动。
- **容错层级变为两道**：协议层 `json_object` 约束为第一道，`_parse_suggested_topics` 手搓容错解析保留为第二道防线（防网关/模型行为差异）；解析失败的各降级路径语义一律不变（spec 对外行为不动，只降失败率）。
- **不支持时姿态（2026-10-12 本人拍板）**：端点对 `response_format` 报 400 时 MUST NOT 做"去掉参数重发"的自动回退，按既有错误链处理（走重试→降级/报错）。理由：生产已实测支持；未来换不支持该参数的提供方时再议。
- **不引新依赖、不动 openai SDK 版本**（`response_format` 为 `chat.completions.create` 既有透传参数）。
- **路径唯一性自查**：本变更不涉及入口/注册/登录/权限/删除/数据归属；AI 调用维持唯一路径——三个方法共用 `_chat` 单一出口加可选参数，无第二条 AI 调用路径（自查见 design.md）。
- **不动的部分**：页面、文案、交互零变化（内部健壮性优化）；`app/local_ai.py` 离线导出/导入路径（无 chat HTTP 调用，response_format 不适用）；各降级路径语义。

## Capabilities

### New Capabilities

（无——纯协议层健壮性增强，不引入新系统行为。）

### Modified Capabilities

- `smart-search`: "LLM 意图理解"与"池内召回与精选"两条 Requirement 的实现容错层级变化——意图理解/意图合并/精选三类调用请求 MUST 带 `response_format={"type":"json_object"}`（协议层第一道），手搓容错解析保留为第二道；端点 400 拒绝该参数时 MUST NOT 自动去参重发。对外降级口径（非法 JSON 退化、精选失败兜底）一字不变。

### 规格自查留痕（裁定 4）

对 `openspec/specs/` 七个 capability 全量自查：`suggest_topics` 无 spec 级解析契约——`topic-classification` 仅在"AI 候选词不直接生效"中提及 AI 归类建议（治理口径，与请求参数/解析无关），`ai-refresh-cadence` 只覆盖 recommend/summary 纯文本链路；均无"剥围栏"类表述需同步。故 specs delta 仅命中 `smart-search`，`suggest_topics` 侧只改实现不改规格。

## Impact

- **代码**：`app/ai.py`——`_chat` 新增可选 `response_format` 参数透传（缺省 None 不传字段）；`suggest_topics` / `understand_intent` / `select_and_reason` 三处调用点传 `{"type": "json_object"}`；`_parse_suggested_topics` 保留不动。
- **测试**：`tests/test_ai.py`——新增断言：三方法请求 payload 含 `response_format={"type":"json_object"}`；纯文本方法（如 translate/recommend）payload 不含该字段；带围栏/杂质 JSON 的容错解析路径回归（第二道防线仍生效）；端点对 response_format 报 400 时不自动去参重发（走既有错误链）。
- **文档**：无产品层冻结物变化（零用户可见口径变更）。
- **数据**：无 schema 变更、无迁移。
- **用户可见口径**：零变化——仅解析失败率与 AI 调用时延/token 消耗下降。
- **外部依赖**：无新增；AI 提供方协议参数（生产 commandcode 网关已实测支持，2026-10-12 留痕）。
- **验证**：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1` 全量绿；部署后生产真实调用一次留痕（归实施阶段任务）。
- **项目 SOP**：触及 AI 外部协议参数 → OpenSpec change 先行（本 change）；零页面/交互变化，免示意图；核心链路（AI 调用链）变更 → 独立评审子 agent。
