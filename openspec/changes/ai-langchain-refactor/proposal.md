# 提案：AI 模块改用 LangChain 重写（ai-langchain-refactor）

## Why

AI 模块 `app/ai.py`（1318 行）底层是 openai 官方 SDK 但手搓了重试循环/错误分类/SSE 流式解析/JSON 容错。本人走查 streaming-search-progress 打回"搜索慢且卡、思维链不实时"，拍板：**AI 模块改用 LangChain 框架重写**，体验口径照搬 love-lens 项目。选型 spike 已完成（产物 `data/ai-fw-spike/result.json`，2026-10-13）：结论选 `langchain-deepseek` 的 ChatDeepSeek 1.1.1——唯一同时满足「`reasoning_content` 流式增量透出」＋「与 `response_format=json_object` 共存」的客户端；`langchain-openai` 的 ChatOpenAI 按设计丢弃第三方 reasoning 字段（源码 docstring 明说），不可用于思维链场景。本变更触及核心链路（AI 调用链）与外部依赖，按项目 §1 口径先建 OpenSpec change。

## What Changes

- **外壳保留、内核替换**：`DeepSeekClient` 保留为全仓唯一 AI 入口，公开方法签名（translate/recommend/summarize/suggest_topics/understand_intent/select_and_reason，含可选 `thinking_cb`）不变，内部实现从 openai SDK 直调换成 ChatDeepSeek；LangChain 模型实例构建收敛为单点 factory。调用方（app/search.py、app/web/routes.py、app/jobs.py）目标零改动。
- **手搓退役清单**：`_chat`/`_chat_stream` 重试循环、`_is_retryable_chat_error`、SSE 增量解析（`_fetch_stream`/`_collect_stream` 的 chunk 手解）、`_reasoning_text` 的 `reasoning_details` 兼容分支（ChatDeepSeek 不认该字段，spike 实证 0 透出）。`_parse_suggested_topics` 容错解析保留（第二道防线，不动）。
- **超时与重试口径（照搬 love-lens，本人拍板）**：流式调用用"60 秒无 chunk 即判挂"的空闲超时（映射 ChatDeepSeek 系 `stream_chunk_timeout`，其缺省 120s）；**交互路径（搜索两段调用，含流式与同步降级路径）不自动重试**——失败如实提示由用户手动重试，**BREAKING**："重试 1 次后放弃"类文案在交互路径消失，属刻意行为变化；**后台批量路径**（每周推荐理由、补译补齐等）保留既有"重试 1 次"口径。连接总上限 300s 防挂死（生产 .env.prod 已显式 `AI_TIMEOUT_SECONDS=300`，本变更把缺省口径对齐）。
- **结构化输出保留**：`response_format={"type":"json_object"}` 保留（生产已实测提速 22.7s→3.8s，2026-10-13 留痕），spike 实证经 `.bind(response_format=...)` 到达请求体且与 reasoning 流式透出共存；流式路径维持"只搬文本增量、末尾一次性解析"形态。
- **错误契约硬口径**：对外异常类型（DeepSeekError/DeepSeekAuthError）与人话文案逐字不变。关键实证：langchain-openai 1.7.1 的封装异常类（OpenAIAuthenticationError/OpenAIRateLimitError/OpenAITimeoutError/OpenAIAPIError 等）全部继承 openai SDK 对应基类，既有六分支分类 isinstance 判定天然覆盖；唯一新增映射是 `StreamChunkTimeoutError`（TimeoutError 系，不在 openai 体系）归入超时分支。
- **前端（smart-search 规格修订，体验口径照搬 love-lens）**：①等待期（首条思维链到达前）立即显示"AI 思考中…"脉冲占位——扭转现状"首 delta 才建思考区"；②搜索中加「取消」按钮（fetch/ReadableStream 消费链已有，AbortController 为浏览器原生 API 直接接入，不引依赖）；③进行中步骤秒数计时保留；④进行中超 15s 无新增量，步骤条下升级提示"网关响应较慢，仍在等待"。
- **配置**：`.env.dev`/`.env.example` 的默认 AI_MODEL 改为 `deepseek/deepseek-v4.1-flash`——免费模型 `stealth/glyph-cluster:free` 对结构化输出报 HTTP 400 且思维链走 `reasoning_details` 数组（ChatDeepSeek 不解析），LangChain 化后不可用作开发模型（spike 实证）；`.env.prod` 本来就是该模型不动。推理档位默认 max 不动（本人明确否定过"max 是慢因"的说法）。
- **依赖**：pyproject 只新增 `langchain-deepseek>=1.1.1`（下界钉 spike 实测版）；langchain-core/langchain-openai 作传递依赖不单独声明；`openai>=3.3,<4` pin 不变（spike 实证共存，openai 3.3.1 未被升降级）。不引 langgraph/agents/编排全家桶。

## Capabilities

### New Capabilities

- `ai-client`: AI 客户端基础设施契约——DeepSeekClient 唯一入口外壳、ChatDeepSeek 引擎选型约束（reasoning 流式透出＋json_object 共存）、流式增量与末尾一次性解析形态、空闲/总上限超时口径、交互/批量双路径重试口径、错误分类六分支与对外异常契约。AI 客户端是跨 smart-search/关注推荐/翻译/主题建议共享的基础设施，其契约独立成档。

### Modified Capabilities

- `smart-search`: 交互路径（搜索两段 LLM 调用，流式与同步降级）不再自动重试（失败如实提示、用户手动重试）；思维链字段解析收敛为只认 `reasoning_content`（`reasoning_details` 兼容退役）；流式加载态新增等待期"AI 思考中…"脉冲占位、「取消」按钮与 ">15s 无增量"慢提示（秒数计时保留）。

### 规格自查留痕

对 `openspec/specs/` 现有七个 capability 全量自查：`ai-refresh-cadence`（推荐语/概要生成节奏）与 `topic-classification`（候选词治理）涉及的 suggest_topics/recommend/translate/summarize 均属后台批量路径，"重试 1 次"口径不变、错误姿态不变，无规格级变化；`language-boards`/`new-project-zone`/`board-hide-tagged`/`tag-categories` 与 AI 调用链无关。`smart-search` 基线取当前 `openspec/specs/smart-search/`＋在途 change `streaming-search-progress` delta 的叠加态（裁定 9：本 change 排在其后归档）。

## Impact

- **代码**：`app/ai.py`——内部实现重写（ChatDeepSeek factory、astream/ainvoke 调用、错误映射），外壳与公开签名不变；手搓清单退役。`app/search.py`/`app/web/routes.py`/`app/jobs.py` 目标零改动（签名不变兜底）。
- **前端**：`app/web/static/radar.js`——等待期占位、取消按钮（AbortController 接入既有 fetch 流）、15s 慢提示；`app/web/templates/search.html`——取消按钮挂载点；`app/web/static/radar.css`——占位脉冲/取消按钮样式。
- **配置**：`.env.dev`、`.env.example` 默认 AI_MODEL 改为 `deepseek/deepseek-v4.1-flash`（`.env.prod` 不动）；`AI_TIMEOUT_SECONDS` 缺省口径对齐 300s（`.env.example` 注释同步）。
- **依赖**：pyproject.toml 新增 `langchain-deepseek>=1.1.1`。
- **测试**：`tests/test_ai.py` 现 87 条逐条对齐新实现语义（错误分支用例经 MockTransport 层重桩保留语义、流式 chunk 桩改为 AIMessageChunk 形态、reasoning_details 分支用例退役、交互路径重试断言反转）。
- **数据**：无 schema 变更、无迁移。
- **用户可见口径**：搜索失败时不再"自动重试 1 次"，改为如实失败提示＋手动重试（刻意变化）；搜索等待期新增占位/取消/慢提示；其余对外行为（意图透明行、降级链、追问语义等）不变。
- **归档顺序**：排在 `streaming-search-progress` 之后归档（后者待本人走查通过＋部署留痕后先归档）。
- **验证**：全套 pytest 绿＋`powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1` 收口一次跑绿；A 级真实环境预演（真网关流式搜索，逐帧到达时刻实测留档 data/）；独立评审子 agent 门禁照项目 §4。
