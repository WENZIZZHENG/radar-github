# Design：AI JSON 产出调用启用 response_format

## Context

现状与动机见 proposal.md。与本设计直接相关的现状约束：

- `app/ai.py` 的 `DeepSeekClient` 所有 AI 调用经 `_chat`（app/ai.py:508）单一出口走 `chat.completions.create`；三个 JSON 产出方法（`suggest_topics` app/ai.py:407、`understand_intent` app/ai.py:429、`select_and_reason` app/ai.py:479）调用 `_chat` 后统一用 `_parse_suggested_topics`（app/ai.py:171）做容错解析。
- `_chat` 的错误链已覆盖：401/403 → `DeepSeekAuthError` 直通；content_policy 403、其他 4xx（含 400）→ `DeepSeekError` 快速失败不重试（`tests/test_ai.py:308` 锁死 4xx fail-fast）；超时/传输错误/5xx/响应畸形重试一次。
- 生产实测（2026-10-12 留痕）：commandcode 网关＋`deepseek/deepseek-v4.1-flash` 支持 `response_format={"type":"json_object"}`（HTTP 200、合法 JSON、3.0s vs 默认 22.7s/1454 reasoning tokens）。本地 .env.dev 的 free 模型（stealth/glyph-cluster:free，同网关）未单独验证该参数（上游当日 408 不稳定）——`response_format` 是 OpenAI 兼容协议标准参数，网关层已验证透传，风险由第二道容错解析兜底。

## Goals / Non-Goals

**Goals:**

- 三个 JSON 产出方法的请求带 `response_format={"type":"json_object"}`，协议层压低非法 JSON 出现率（第一道防线）。
- 纯文本产出方法（translate/recommend/summarize）的请求体逐字节不变（回归可断言）。
- 容错层级：协议层约束 → 手搓容错解析（第二道，保留）→ 既有降级路径（语义不变）。

**Non-Goals:**

- 不改 prompt 任何一字；不改 `_parse_suggested_topics` 解析逻辑；不改任何降级路径语义。
- 不做"端点 400 → 去掉参数自动重发"的回退（本人拍板，理由见 proposal）。
- 不引新依赖、不动 openai SDK 版本、不改配置项（不新增开关/环境变量）。
- 不动 `app/local_ai.py` 离线导出/导入路径（无 chat HTTP 调用，`response_format` 不适用）。

## Decisions

### 决策 1：实现落点——`_chat` 加可选参数透传，三方法调用点传值

`_chat` 签名加 `response_format: dict | None = None`：非 None 时透传给 `chat.completions.create`；None（缺省）时**不传该字段**（而非传 None），保证纯文本方法请求体逐字节不变。三个 JSON 方法各自在调用点显式传 `{"type": "json_object"}`。

- 备选 A：三方法绕过 `_chat` 直接调 `self._client.chat.completions.create`——否决：破坏"AI 调用唯一出口"（重试/错误链/key 校验全在 `_chat`），等于开第二条调用路径。
- 备选 B：`_chat` 加 `json_mode: bool` 布尔参数，内部拼字典——否决：布尔参数把协议细节（`{"type":"json_object"}` 的字面形态）藏进出口内部，未来若需 `json_schema` 形态还得再改签名；透传 dict 更直白。
- 备选 C：默认对所有调用都带 json_object——否决：纯文本方法会被协议强制返回 JSON，直接改坏 translate/recommend/summarize。

### 决策 2：端点 400 不做自动回退（本人拍板，起草不改动）

端点对 `response_format` 报 400 时落入既有 4xx fail-fast 链（`DeepSeekError`），调用方按既有姿态处理（搜索页不可用提示 / 精选退化 / suggest_topics 整段跳过），语义一字不变。生产已实测支持；未来换不支持该参数的提供方时再议回退策略（届时需新 change 拍板）。

### 决策 3：`_parse_suggested_topics` 手搓容错解析原样保留

协议层 `json_object` 是"约束模型输出形态"，不保证网关/代理不改写内容（实证：部分网关会在 JSON 外套围栏或注入前后缀）。第二道防线成本为零（代码已存在且有测试），拆除反而引入对单一提供方行为的隐式依赖。

### 决策 4：specs delta 仅命中 smart-search（规格自查结论）

`openspec/specs/` 七个 capability 全量自查：`suggest_topics` 无 spec 级解析契约（`topic-classification` 只在治理口径提及"AI 候选词不直接生效"，与请求参数/解析无关；`ai-refresh-cadence` 只覆盖纯文本链路）。故 `suggest_topics` 的请求参数变化只改实现、不进 delta；smart-search 的"LLM 意图理解"与"池内召回与精选"两条 Requirement 以 MODIFIED 承载容错层级变化，降级口径文字不动。

### 路径唯一性自查

- 几条路径：AI chat 调用仍只有一条——`DeepSeekClient._chat`；本变更在其上加可选参数，不开第二路径。`app/local_ai.py` 是离线导出/导入链路（任务包渲染＋结果文件导入），无 chat HTTP 调用，与本变更正交。
- 语义是否唯一：唯一。三个 JSON 方法显式声明自己要 JSON 产出（传参在调用点可见），纯文本方法缺省不传，两类的请求形态在代码上一眼可辨。
- 入口/注册/登录/权限/删除/数据归属：均不涉及。

## Risks / Trade-offs

- [本地 free 模型（stealth/glyph-cluster:free）未实测该参数，若其上游不支持则本地开发的三方法调用 400] → 第二道容错解析救不了 400（400 是调用失败非解析失败），但按拍板口径不自动回退；本地开发可用生产同源账号或接受搜索/建议功能本地不可用（榜单主链路不受影响）；验证任务含"若本地 free 模型实测 400 则如实记录，不视为验证失败"。
- [`json_object` 模式下个别模型对"只输出 JSON"的 prompt 依赖仍在（如 OpenAI 官方要求 prompt 含 "json" 字样，部分兼容网关同）] → 三个方法的现有 prompt 均含"只输出一个 JSON 对象"字样，满足约束；prompt 一字不改的拍板与此兼容。
- [reasoning token 消耗结构变化（实测 1454 → 近零）可能伴随输出风格漂移] → 只影响 JSON 内容质量不影响解析；精选/意图的字段合法性校验（编号范围、语言白名单、filters 白名单）全在调用方既有逻辑，漂移被既有校验兜住；部署后生产真实调用一次留痕（实施阶段任务）作为最终确认。

## Migration Plan

无迁移：无 schema/配置/数据变更。部署＝代码上线重启服务；回滚＝回退 commit 重启（`response_format` 是请求级参数，无持久状态）。部署后按 tasks.md 执行生产真实调用一次留痕。
