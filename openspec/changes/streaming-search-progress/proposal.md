# 提案：搜索流式进度与思维链显示（streaming-search-progress）

## Why

搜索页（/search，P8）当前为表单 POST 同步 SSR：提交后页面干等完整检索链路（两次 DeepSeek 调用串行，典型 10~30 秒），加载态只有按钮置灰"搜索中…"（radar.js:1221-1232）——等待期间用户不知道是在干活还是卡住了，也看不到 AI 在想什么。生产 AI 提供方（commandcode 网关＋`deepseek/deepseek-v4.1-flash`）思维链随响应返回（字段 `reasoning_content`），具备流式边生成边显示的条件。本人已拍板 **B 档：流式化**——等待期间实时可见"在干什么"：分阶段进度＋AI 思维链原文流式显示。

## What Changes

- **新增流式搜索端点**：`POST /search/stream`（FastAPI StreamingResponse 服务端流式推送），事件序列＝分阶段进度（理解意图 → 池内召回 → AI 精选 → 补搜 GitHub）＋两段 LLM 调用的思维链增量＋最终 SSR 渲染好的结果区 HTML 片段。帧格式与 GET/POST 选型论证见 design.md（结论：POST＋fetch ReadableStream，NDJSON 帧）。
- **前端流式加载态**（radar.js，vanilla 不引框架/构建工具）：表单提交改为 fetch 流式消费；新增进度面板——阶段步骤条（pending/active/done）＋"AI 思考过程"区（英文原文流式追加，两段调用分段显示，完成后可折叠）；无 reasoning 字段时退化为纯阶段进度。
- **既有同步 `POST /search` 路由原样保留**作无 JS/降级路径：JS 不可用、fetch/ReadableStream 不可用、流中途失败三种情况均收敛回同步表单提交；两条路径跑同一 `run_search` ＋同一结果区 partial 模板，结果语义唯一（只差加载态，路径唯一性自查见 design.md）。
- **结果行形态不破**：流式端点复用 `_row.html` SSR 渲染结果片段下发（前端不重复实现行模板）；搜索页结果区模板抽为公共 partial，同步/流式两路径共用单一事实源。
- **AI 客户端新增流式调用能力**：`DeepSeekClient` 增流式变体（`stream=True`），思维链解析兼容两种字段名——`reasoning_content`（字符串增量，生产 deepseek 实测）与 `reasoning_details`（数组形态，本地 free 模型）；两者皆缺 → 不下发思维链事件（纯阶段进度）。思维链为英文原文如实显示，不承诺中文。
- **对外行为零变化**：意图透明行、degraded 退化、精选失败粗排直出、补搜失败标注、空结果说明、追问语义、无状态回传口径——smart-search spec 既有 Requirement 一字不改，本变更只 ADDED 一条"流式加载态" Requirement。
- **不建搜索缓存**（现有口径不破）；同步路径的请求形态（表单字段/校验/降级链）一字不动。

## Capabilities

### New Capabilities

（无——流式加载态是 smart-search 既有能力的加载态维度，不独立成新 capability。）

### Modified Capabilities

- `smart-search`: ADDED Requirement"搜索流式加载态与思维链显示"——搜索执行等待期间系统 SHALL 流式下发分阶段进度与 AI 思维链增量，并在结束时下发与同步路径一致的 SSR 结果片段；流式不可用/失败时 MUST 收敛回既有同步路径（结果语义唯一）。既有 Requirement 全部不动。

### 规格自查留痕

对 `openspec/specs/` 七个 capability 全量自查：加载态/传输形态仅搜索页涉及（榜单/关注/标签等页面无 AI 等待链路，`ai-refresh-cadence` 管生成节奏、`topic-classification` 管词表治理，均与"用户等待期间可见进度"无关）。故 specs delta 仅命中 `smart-search`，且只 ADDED 不 MODIFIED（裁定 6：既有对外行为一字不改）。

## Impact

- **代码**：`app/ai.py`——新增流式 chat 变体（`stream=True`＋两种 reasoning 字段解析），`understand_intent`/`select_and_reason` 增可选思维链回调（缺省 None 时走既有 `_chat`，同步路径请求形态逐字节不变）；`app/search.py`——`run_search` 增可选进度/思维链回调（缺省 None 时行为与现状一致）；`app/web/routes.py`——新增 `POST /search/stream` 端点（复用既有表单解析/`_search_view`）；`app/web/templates/search.html`——结果区抽公共 partial（同步/流式共用），新增进度面板挂载点；`app/web/static/radar.js`——搜索提交改流式消费＋进度面板渲染＋降级收敛；`app/web/static/radar.css`——进度面板/思维链区样式（原型确认后迁入）。
- **测试**：`tests/test_ai.py`（流式变体两种 reasoning 字段解析、缺字段退化、错误链姿态）；`tests/test_search.py`（run_search 回调触发时序、缺省零回调行为不变）；`tests/test_web.py` 或新增（流式端点事件序列、与同步路径结果一致性、表单校验口径一致）。
- **文档**：`docs/sop/交互流程/17-智能搜索.md` 新增流式加载态小节（本 change 前置确认物）；同保真原型 `docs/sop/原型-T041.html`（新交互模式，项目 §2 分档：原型本人确认后开发）。
- **数据**：无 schema 变更、无迁移、无缓存。
- **用户可见口径**：仅加载态——等待期间可见阶段进度与思维链；结果内容与形态与现状一致。
- **外部依赖**：无新增（openai SDK `stream=True` 为既有能力）；AI 提供方流式行为（生产网关流式＋`response_format` 组合下 reasoning_content 是否非空待生产实测，缺则退化纯阶段进度——见 design.md 风险）。
- **验证**：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1` 全量绿；核心链路（搜索）变更 → 独立评审子 agent；A 级真实环境预演（真网关流式调用）。
- **项目 SOP**：改用户可见交互（新交互模式）→ OpenSpec change 先行（本 change）＋同保真原型本人确认＋《交互流程说明》§17 文字层前置确认。
