# Design：AI 模块改用 LangChain 重写

## Context

现状与动机见 proposal.md。与本设计直接相关的现状约束与已核实事实（均读码/spike 实证）：

- **现状实现**：`DeepSeekClient`（app/ai.py:363）以 AsyncOpenAI 直调 chat/completions，手搓层包括：`_chat`（app/ai.py:619）重试循环＋`_chat_stream`（app/ai.py:690）SSE 增量解析＋`_handle_chat_error`（app/ai.py:777）六分支错误分类＋`_reasoning_text`（app/ai.py:199）双字段思维链解析。公开方法：translate/recommend/summarize/suggest_topics/understand_intent/select_and_reason（后两者带可选 `thinking_cb`，T-041 流式化已落地）。
- **调用方三处**：`app/web/routes.py:1520`（`_build_ai_client`，请求级与批量 worker 共用）、`app/jobs.py:135`（每日任务）、`app/search.py`（检索链路经 `run_search` 透传 thinking_cb）。三处构造参数集一致（api_key/base_url/model/request_timeout/max_retries）。
- **spike 实证**（`data/ai-fw-spike/result.json`，2026-10-13，真网关 `deepseek/deepseek-v4.1-flash`）：ChatDeepSeek 1.1.1 流式/非流式均从 `additional_kwargs["reasoning_content"]` 透出思维链（langchain-core content_blocks 同步镜像 reasoning/text），且与 `response_format=json_object` 共存（case2 流式 json：reasoning 49 字符＋正文 JSON 合法）；ChatOpenAI 两路径 reasoning 透出均为 0（设计丢弃第三方字段）。`response_format` 经 `.bind(response_format=...)` 到达请求体（probe.py:452 机制＋抓包核对）。
- **LangChain 异常体系实测**（.venv 内 langchain-openai 1.7.1 MRO 核验）：`OpenAIAuthenticationError`→openai.AuthenticationError、`OpenAIPermissionDeniedError`→PermissionDeniedError、`OpenAIRateLimitError`→RateLimitError、`OpenAIAPIError`→InternalServerError、`OpenAITimeoutError`→APITimeoutError、`OpenAIConnectionError`→APIConnectionError——**全部继承 openai SDK 对应基类**；未被 `_handle_openai_api_error` 特殊包装的状态码（402/400/502/503/504 等）原样上抛 openai 原生异常。唯一体系外的是 `StreamChunkTimeoutError`（TimeoutError→OSError，空闲超时掐断）。
- **ChatDeepSeek 构造面实测**：字段 `api_base`（非 base_url）、`model_name`（alias model）、`request_timeout`、`max_retries`（缺省 2，透传 openai SDK 内置重试）、`stream_chunk_timeout`（继承 BaseChatOpenAI，缺省 120s，无 chunk 掐断抛 StreamChunkTimeoutError）、`http_async_client`/`http_client`（测试注入面）、`model_kwargs`。
- **langchain-core 1.6.10 `with_retry`**：`Runnable.with_retry(retry_if_exception_type=..., wait_exponential_jitter=..., exponential_jitter_params=..., stop_after_attempt=...)`，返回 RunnableRetry（ainvoke 可用）；`wait_exponential_jitter=False` 时 tenacity 不挂等待策略＝零等待立即重试。
- **配置现状**：`.env.prod` 已显式 `AI_TIMEOUT_SECONDS=300`；`.env.dev` 未设该项（走缺省 60）；`.env.dev` 现值模型 `stealth/glyph-cluster:free` 对 response_format 报 400 且思维链走 `reasoning_details`（spike 实证不可用作开发模型）。
- **前端现状**（streaming-search-progress 已落地，本变更基线）：radar.js 已有 fetch＋ReadableStream NDJSON 消费链、四阶段步骤条（含秒数计时）、思考区（首 delta 才建区）、失败收敛自动重跑；无 AbortController 接入（fetch 未带 signal）。
- **测试现状**：`tests/test_ai.py` 现收集 87 条（任务书口径"78 条"为 streaming-search-progress 合入前数字，以实测为准）。

## Goals / Non-Goals

**Goals:**

- `DeepSeekClient` 外壳与公开签名不变，内部实现换成 ChatDeepSeek；模型实例构建收敛为单点 factory；调用方（search.py/routes.py/jobs.py）零改动。
- 重试双口径落地：交互路径零自动重试；批量路径保留"重试 1 次"且可重试白名单语义不变（429/4xx/账户类不重试）。
- 超时口径落地：流式空闲超时 60s、连接总上限 300s。
- 错误契约：对外异常类型与人话文案逐字不变（唯一例外＝交互路径"重试 N 次后放弃"类文案消失）。
- 前端四处：等待期"AI 思考中…"脉冲占位、取消按钮、秒数计时保留、>15s 慢提示。

**Non-Goals:**

- 不引 langgraph/agents/编排组件；不改 prompt 任何一字；不动 `_parse_suggested_topics` 容错解析；不改各降级路径语义（意图失败→unavailable、精选失败→粗排直出等）。
- 不动 `app/local_ai.py` 离线导出/导入路径（无 chat HTTP 调用，正交）。
- 不改检索链路语义与事件协议（NDJSON 帧结构不变；取消/占位/慢提示为纯前端行为）。
- 不做推理档位调整（max 档不动，本人已拍板）；不做"端点 400 去参重发"回退（既有拍板沿用）。
- 不重建 `streaming-search-progress` 已交付的流式基础设施（本变更在其合入态上叠加）。

## Decisions

### 决策 1：外壳保留，内部收敛为单点 factory（双实例：交互/批量）

`DeepSeekClient.__init__` 签名保持不变（api_key, base_url, model, request_timeout, transport, sleep, max_retries）——routes.py/jobs.py 调用点零改动由签名稳定兜底。内部新增单点 factory `_build_chat_model(...)`，产出两个 ChatDeepSeek 实例：

- **交互实例**：`max_retries=0`（关 LC 内置重试）、`stream_chunk_timeout=60`、`request_timeout`（缺省 300），**不包任何重试**；服务 understand_intent/select_and_reason（流式与同步降级形态同实例）。
- **批量实例**：基础参数同上，`max_retries=0`，外层包 `with_retry`（决策 2）；服务 translate/recommend/summarize/suggest_topics。

构造参数映射：facade `base_url`→`api_base`、`model`→`model`、`request_timeout`→`request_timeout`、`transport`→包进 `httpx.AsyncClient` 作 `http_async_client` 注入（测试缝保留，MockTransport 收到的请求 URL 仍为 `{base_url}/chat/completions`，与现状一致）。temperature 与 response_format 不进构造器，由方法调用点经 `.bind(...)` 下发（spike 实证 .bind 到达请求体；各方法 temperature 现状值不变：0.2/0.2/0.3 等）。

**补记（2026-10-14 主 agent 裁定 1：流式形态的 response_format 改经 `extra_body` 下发）**：顶层 `bind(response_format=...)` 会命中引擎的结构化输出解析路径（langchain-openai `_astream`：`if "response_format" in payload → beta.chat.completions.stream()`），逐 chunk 思维链被引擎丢弃、只在流末聚合块一次性透出，违背本 change"思维链实时"初衷。实证链：①spike `result.json` case2（流式 json_object）`loc_chunk_counts["additional_kwargs.reasoning_content"]=1`、`first_reasoning_chunk_s=4.2144 ≈ total_stream_s=4.215`；②实施后冒烟 `smoke-impl-result.json`（改进前）thinking_increments=1、唯一增量 at_s=3.705/总 3.707s；③MockTransport 反事实对照：顶层绑定 → 1 条聚合（`['第一段第二段第三段']`），`extra_body` 绑定 → 3 条逐段。故 `_stream_chat`（thinking_cb 非 None 的 astream 路径）改用 `_stream_bind_kwargs`：`bind(temperature=..., extra_body={"response_format": ...})`——由 SDK 合并进请求体，**线上请求体不变**（顶层仍带 response_format，MockTransport 断言锁死），引擎走普通流式路径，逐段透出恢复。**非流式路径维持顶层 `.bind(response_format=...)` 不动**（冒烟已证解析正确，且无逐 chunk 需求）。改进后真网关复测：increments=49、首条 2.397s/总 3.705s（ratio 0.647，中段分布）。回归守卫：`test_chat_stream_multi_increments_with_response_format`（>1 条逐段＋请求体仍带该参数）。

**补记（2026-10-14 主 agent 裁定 2：依赖上界）**：`StreamChunkTimeoutError` 只能从 langchain-openai 私有模块（`langchain_openai.chat_models._client_utils`）导入，且是 `app.ai` 模块导入期硬依赖——`pyproject.toml` 的 `langchain-deepseek` 加下界加上界 `>=1.1.1,<2`（与 `openai>=3.3,<4` 同款大版本漂移防护风格）。

- 备选 A：单实例＋调用点临时包 with_retry——否决：每次调用重建 Runnable 包装属无谓开销，且"哪条路径什么口径"在代码上不如双实例一眼可辨。
- 备选 B：改签名拆分两个 Client 类——否决：违反"外壳唯一入口/调用方零改动"裁定。
- 备选 C：保留 openai SDK 作底层——否决：裁定 1 已钉 LangChain 化。
- 备选 D（裁定 1 的替代方案）：不传 response_format 只靠第二道防线容错解析——否决：协议层 JSON 约束是既有拍板（生产提速 22.7s→3.8s 留痕），不得为流式让路。

### 决策 2：重试双口径映射（必答 2）

LangChain 体系有三层重试可动：openai SDK 内置（ChatDeepSeek `max_retries` 透传，缺省 2，指数退避且重试 429）、LC 内置（同字段）、`with_retry` 显式包装。映射结论：

1. **LC/SDK 内置重试一律关闭**（两实例均 `max_retries=0`）——其语义（重试 429＋指数退避）与本仓口径（429 不重试、固定节奏、仅批量重试）冲突，不关则双重复试放大调用量（现状 `max_retries=0` 关 SDK 自带重试的同理由延续）。
2. **交互路径**：不包 `with_retry`，任何失败即抛对外异常（同步降级形态同——两条传输形态同口径，spec 已钉）。
3. **批量路径**：`with_retry(stop_after_attempt=self._max_retries + 1, retry_if_exception_type=RETRYABLE_EXC, wait_exponential_jitter=False)`。`RETRYABLE_EXC` 为模块级白名单常量，枚举与现状 `_is_retryable_chat_error` 判级等价：`(openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError, openai.BadGatewayError, openai.ServiceUnavailableError, openai.GatewayTimeoutError, StreamChunkTimeoutError, _ResponseMalformedError)`——isinstance 匹配天然覆盖 LC 包装子类（OpenAITimeoutError 等，Context 已核实 MRO）；429/400/401/402/403 不在名单，永不重试。
4. **响应畸形**（ainvoke 返回内容缺失/为空）：外壳自检后抛内部 `_ResponseMalformedError(DeepSeekError)`——子类化使对外仍见 DeepSeekError（文案"DeepSeek 响应畸形…"不变），同时可进 `RETRYABLE_EXC` 白名单复刻现状"畸形重试一次"；交互路径不包 with_retry 故直接抛。

**补记（2026-10-14 主 agent 裁定 F2-1：引擎解析层拒收响应体归一到 `_ResponseMalformedError`）**：旧实现里"响应体无法解析"落 `APIError` 分支——文案"DeepSeek 响应畸形（APIError: …）"＋批量路径重试一次；引擎切换后同样的响应在引擎内部就抛异常、落"未知异常"分支（文案变"请求失败（…）"且批量不再重试），双重漂移。修法：引擎调用点（最小面）捕获解析层异常归一到 `_ResponseMalformedError`——文案恢复"响应畸形"、经白名单恢复批量重试一次；`retry_if_exception_type` 无需改动（该类已在 `RETRYABLE_EXC`）。**捕获类型按实测取证取三型 `_ENGINE_PARSE_ERRORS = (ValueError, AttributeError, TypeError)`**（裁定原文只点名 ValueError，实操发现带 response_format 的调用走 openai SDK 结构化输出解析面、抛的是 AttributeError/TypeError 而非 ValueError，仅兜 ValueError 达不到裁定验收判据；已如实回主 agent）：无 response_format → LangChain 响应类型检查抛 ValueError；带 response_format → openai `_parse_chat_completion` 遍历 str 的 `.choices` 抛 AttributeError、200＋`{"error": …}` 体抛 TypeError。边界：参数构造全部移出捕获面（本模块自身代码不在其中），网络层失败是 APIConnectionError/APITimeoutError 等特定类型、不在三型内；批量重试单元随之改为 `RunnableLambda(self._batch_once)`（引擎调用＋解析层归一＋自检都在 with_retry 内，畸形才走得上重试）。回归测试：批量＋response_format 两形态桩（非 JSON 体／error 体）断言"响应畸形"＋计数==2、无 response_format 对照（ValueError 面）计数==2、交互同类失败计数==1。

**等待节奏偏差（存疑点 1）**：现状批量重试前固定 sleep 1s（`RETRY_WAIT_SECONDS`）；`with_retry(wait_exponential_jitter=False)` 为零等待立即重试。tenacity 等待参数表达不出"固定 1s"（True＝指数＋抖动）。本设计暂定接受零等待（重试仅 1 次且为低频异常路径，"立即重试一次"语义偏差小），`sleep` 注入参数随之失去消费点但签名保留。若须精确保留 1s 等待，退路＝批量路径用约 10 行薄包装（try/一次重试/`self._sleep(1.0)`）替代 with_retry——不属裁定 2 退役的 `_chat` 重试循环（那个循环耦合了错误分类与畸形判定）。**请主 agent 拍板。**

### 决策 3：思维链透出接法（必答 3）

- 字段来源：`chunk.additional_kwargs["reasoning_content"]`（spike 实证唯一透出点；content_blocks 的 reasoning 镜像块为等价读法，取其一即可，设计钉 additional_kwargs 与 spike/既有 `_reasoning_text` 优先级一致）。
- 回调签名：**不动**。`understand_intent`/`select_and_reason` 的 `thinking_cb: Callable[[str], None] | None` 原样保留（search.py 已按 stage 包装成单 delta 回调，routes.py/search.py 侧零改动）。
- 接法：`thinking_cb is None` → `ainvoke`（同步降级路径，交互口径不重试）；非 None → `async for chunk in model.astream(...)`，每个 chunk 取 `additional_kwargs.get("reasoning_content")`，为 str 且非空即 `thinking_cb(增量)`；正文取 `chunk.content`（spike 实测为 str；防御：list 形态时拼接其中 text 块）累积，流结束后一次性 `_parse_suggested_topics`（"只搬文本增量、末尾一次性解析"形态不变，第二道防线不动）。
- `reasoning_details` 兼容分支随 `_reasoning_text` 整体退役（ChatDeepSeek 不解析该字段，死代码）。
- 取消支持：前端 AbortController 中断 fetch → FastAPI 断连 → 流式生成器被取消，astream 迭代随连接关闭终止，无需额外接线（uvicorn 请求生命周期兜底）。

### 决策 4：错误分类六分支映射表（必答 4）

外壳保留 `_handle_chat_error` 等价单点（判级逻辑与方法归位，但不再驱动重试循环——重试职责移交 with_retry/交互零重试）。逐分支对应（MRO 已实测核验）：

| 分支 | 现状判定（openai SDK） | LangChain 1.7.1 实际抛出 | 处置 |
| --- | --- | --- | --- |
| 401 | AuthenticationError | OpenAIAuthenticationError（继承之） | isinstance 天然命中 → DeepSeekAuthError 直通，文案不变 |
| 403 content_policy | PermissionDeniedError＋标记判定 | OpenAIPermissionDeniedError（继承之） | 天然命中；`_is_content_policy_403` 判定保留 → DeepSeekError 单条跳过 |
| 403 普通 | PermissionDeniedError | 同上 | 天然命中 → DeepSeekAuthError |
| 402 | APIStatusError(status==402) | openai 原生原样上抛（LC 不包装） | 天然命中 → DeepSeekAuthError |
| 429 | RateLimitError | OpenAIRateLimitError（继承之） | 天然命中 → DeepSeekError 不重试（白名单外） |
| 其他 4xx（含 400） | BadRequestError 等 APIStatusError | 原生原样上抛 | 天然命中 → DeepSeekError 不重试；400 不去参重发（既有拍板） |
| 5xx | InternalServerError/BadGatewayError/ServiceUnavailableError/GatewayTimeoutError | 500→OpenAIAPIError（继承 InternalServerError）；502/503/504 原生原样 | 天然命中 → 批量重试一次后 DeepSeekError／交互直接 DeepSeekError |
| 超时 | APITimeoutError | OpenAITimeoutError（继承之） | 天然命中 → 超时分支 |
| 连接 | APIConnectionError | OpenAIConnectionError（继承之） | 天然命中 → 连接分支 |
| 空闲超时（新增概念） | 无（手搓时代无此机制） | **StreamChunkTimeoutError（TimeoutError 系，体系外）** | **唯一新增显式 isinstance 映射** → 归超时分支（批量可重试白名单含之） |
| 响应畸形 | APIError／content 缺失自检 | LC 解析层异常原样透传；content 缺失外壳自检 | `_ResponseMalformedError(DeepSeekError)` 承载（决策 2-4） |
| 未知异常 | 兜底 DeepSeekError | 同 | 不变 |

文案落点：所有 `DeepSeekError`/`DeepSeekAuthError` 人话文案逐字保留；交互路径因不再重试，"重试 N 次后放弃"类文案仅批量路径可达（`self._max_retries` 语义随之收窄为"批量路径重试次数"，缺省仍走 `AI_MAX_RETRIES`=1）。

### 决策 5：超时口径落实

- 空闲超时：`stream_chunk_timeout=60` 写死常量（S 档不增生配置面；LC 缺省 120s 不沿用）。
- 连接总上限：`request_timeout` 缺省 300s——`app/config.py` 的 `DEFAULT_AI_TIMEOUT_SECONDS` 60→300，`.env.example` 注释同步（"单次调用超时（秒）"缺省值描述改 300）。`.env.prod` 已显式 300 不受影响；`.env.dev` 未设该项，跟随新缺省即对齐裁定 4。环境变量覆盖能力保留（换提供方时的逃生门）。
- ChatDeepSeek 构造参数名 `request_timeout` 与 facade 参数同名，映射直白。

### 决策 6：前端四处（smart-search 规格修订的落地形态）

1. **等待期占位**：`setSearchStep` 在 intent/select 段 `start` 时即渲染思考区该段骨架＋"AI 思考中…"脉冲占位（扭转现状 `appendSearchThinking` 首 delta 才建区）；首条 thinking 事件到达时清占位、原文追加；段收尾（done/result 帧）时若该段无任何增量则移除骨架（退化口径：不留空架）。
2. **取消按钮**：进度面板加「取消」；每次 `runStreamSearch` 新建 AbortController，`fetch(..., {signal})`；点击 → `controller.abort()` → 读流抛 AbortError → catch 分支**区分 AbortError 与真失败**：AbortError → 清进度区、如实提示"已取消"、恢复提交态，**不走 `failSearchStream`**（不自动重跑——取消非失败）；其余错误走既有失败收敛不变。
3. **秒数计时保留**：现状机制（每秒跳动、终态停表）一行不动。
4. **15s 慢提示**：段 `start` 启动 watchdog，任一 stage/thinking 事件到达即重置；超 15s 无事件在步骤条下显示"网关响应较慢，仍在等待"；事件恢复或段终态时消退。纯前端计时，不改事件协议（与秒数计时同原则）。

样式（脉冲动画/取消按钮/慢提示条）沿用项目惯例：先在原型或页面 `<style>` 确认后迁 radar.css。

### 决策 7：测试移植策略（必答 5，87 条逐条对齐口径）

- **不动（约 60 条）**：全部 `conn` 参数化的批量编排测试（FakeDeepSeekClient 替身，不触真实客户端——translate/recommend/summary/window/backfill/auth 直通等）＋prompt frozen literal 系列＋`test_client_methods_delegate_to_build_prompt`。这些测的是编排语义与 prompt 文本，与底层引擎无关。
- **语义保留、桩层适配（约 15 条）**：HTTP 层错误分支与 payload 断言类——统一改用 MockTransport 桩 HTTP/SSE 层（ChatDeepSeek 经 `http_async_client` 注入，请求仍真实穿过 openai SDK→LC 包装→外壳分类，链路最真）：`test_client_posts_chat_completions_payload`、`test_client_retries_5xx_once_then_succeeds`（translate 属批量，重试一次语义不变）、`test_client_4xx_fails_fast_without_retry`、`test_client_401_raises_auth_error_without_retry`、`test_client_403_content_policy...`、`test_client_403_plain_still_auth_error`、`test_client_malformed_json_retried_once_then_raises`（批量畸形重试一次）、`test_client_timeout_retried_once_then_raises`（批量）、`test_client_empty_key_fails_before_any_request`、三条 `*_posts_json_response_format`、`test_translate_payload_has_no_response_format`、`test_response_format_400_fails_fast_without_stripping_param`、 fenced-JSON 容错三条。
- **改写（约 8 条流式类）**：`test_chat_stream_reasoning_content_and_content_accumulate`（桩改 SSE 字节流或 AIMessageChunk 形态，断言 reasoning_content 逐段喂回调＋content 累积不变）；`test_chat_stream_without_reasoning_fields_degrades_silently`（同适配）；`test_understand_intent_and_select_and_reason_thinking_cb_streams`、`test_thinking_cb_default_keeps_sync_request_byte_identical`（同步降级路径 payload 断言：无 stream 字段＋带 response_format，MockTransport 层可断言）；`test_chat_stream_interrupted_after_delta_raises_without_retry`＋`..._message_honest_no_retry_claim`（语义保留：中断不重试＋文案如实，桩适配）。
- **语义反转（退役旧断言、写新断言）**：`test_chat_stream_retries_before_first_byte_then_succeeds` → 反转为"交互路径首字节前失败也不重试（请求计数==1）"；`test_chat_stream_empty_content_retries_then_raises` → 反转为"空内容不重试直接抛"。新增：understand_intent/select_and_reason 同步降级路径遇 5xx/超时只发 1 次请求直接抛。
- **退役**：`test_chat_stream_reasoning_details_array_form`（reasoning_details 兼容分支退役，死代码无测试对象）。
- **新增**：StreamChunkTimeoutError → 超时分支映射用例；交互/批量双实例重试口径对照用例（交互 5xx 计数==1、批量 5xx 计数==2）。

**补记（2026-10-14 主 agent 裁定 1 的测试补充）**：新增 `test_chat_stream_multi_increments_with_response_format`——真实链路（MockTransport）锁"流式＋response_format 下 thinking_cb 收到 **>1 条**逐段增量"＋"线上请求体仍带顶层 response_format"双断言（防"流末一次性"退化回归）；`test_chat_stream_reasoning_content_and_content_accumulate` 的 bind 断言同步改为 `extra_body` 形态。实施留痕：静止桩三增量 → 修复前 1 条聚合／修复后 3 条逐段；真网关复测 increments=49、首条 2.397s/总 3.705s。测试收集数 89→**90**。

**实施结果与本文清单的差异登记（2026-10-14）**：①`test_thinking_cb_default_keeps_sync_request_byte_identical` 改名为 `test_thinking_cb_default_keeps_sync_request_shape`——LangChain 非流式请求恒显式下发 `stream: false`，"无 stream 字段"断言达不到（语义仍是非流式），断言改 `body["stream"] is False`；②`test_client_malformed_json_retried_once_then_raises` 桩改"响应缺 content/空白"形态并改名 `test_client_malformed_response_retried_once_then_raises`——该条覆盖外壳自检可判定的畸形面；引擎解析层拒收响应体的另一面由 F2-1 用例组覆盖（见决策 2 补记）；③退役 1 条＋新增 3 条（含裁定 1 回归）后收集数 87→90，再加 F2-1 用例组 4 条 →**94**（`test_ai.py`）；④`RETRYABLE_EXC` 只列 `InternalServerError` 覆盖 5xx——openai 3.3.1 无 `BadGatewayError`/`ServiceUnavailableError`/`GatewayTimeoutError` 三个类（已核），502/503/504 实测落 `OpenAIAPIError(InternalServerError)`；⑤批量 with_retry 包"引擎调用＋解析层归一＋响应自检"的重试单元（`RunnableLambda(self._batch_once)`）而非裸模型——畸形由外壳在 ainvoke 返回后判定，只包模型则畸形不会重试，与决策 2-4 冲突（F2-1 后该单元同时承载解析层 ValueError/AttributeError/TypeError 归一）；⑥死代码清理扩至 `_extract_content`/`_response_text`/`_delta_field`/`RETRY_WAIT_SECONDS`（随解析层退役失去消费点）；⑦F3-1 流式零 content 文案逐字回旧："DeepSeek 流式响应畸形（未产生任何 content 增量）"（异常类型同回 plain `DeepSeekError`）。

## 路径唯一性自查（必答 1）

- **几条入口路径**：AI 调用入口唯一＝`DeepSeekClient`。内部按两维展开：传输维（非流式 ainvoke／流式 astream，由 `thinking_cb` 是否为 None 单点分支）× 场景维（交互＝understand_intent/select_and_reason；批量＝translate/recommend/summarize/suggest_topics）。组合共四条：**交互×流式**（搜索流式路径）、**交互×非流式**（搜索同步降级路径）、**批量×非流式**（每日任务/补译/候选词扫描）、**批量×流式**（不存在——批量方法无 thinking_cb 参数，结构性为空）。
- **语义是否唯一**：唯一。重试口径按场景维一刀切成两档（交互零重试／批量重试一次），与传输维正交——交互的两条传输形态同口径（spec 已钉"流式与同步降级形态同"）；每条方法的重试姿态由其所用实例（决策 1 双实例）构造级保证，不存在"同一方法两种重试语义"的缝隙。`app/local_ai.py` 离线导出/导入链路无 chat HTTP 调用，与本变更正交（沿用既有结论）。
- 入口/注册/登录/权限/删除/数据归属：均不涉及（AI 客户端是内部基础设施，无数据归属变化）。

## Risks / Trade-offs

- [LangChain 传递依赖面扩大（langchain-core/langchain-openai＋17 个传递依赖）→ 供应链与版本漂移面变大] → 只直申 `langchain-deepseek>=1.1.1,<2`（下界钉实测版、上界防大版本漂移，裁定 2）；openai `>=3.3,<4` pin 不动（spike 实证共存）；不引编排组件；传递依赖由 pip 解析自然收敛。
- [with_retry 零等待 vs 现状固定 1s 的偏差（存疑点 1）] → 低频异常路径、重试仅一次，影响面小；已列存疑点待拍板，退路（薄包装）成本约 10 行。
- [ChatDeepSeek 对网关异常行为的包装差异（如网关返回非标准错误体）→ 六分支判级可能落入"未知异常"] → MockTransport 层测试按真实网关错误体形态桩（401/403/429/500/畸形），每分支一条用例锁死映射；A 级预演真网关验证。**已实证补充（F2-1）**：200 但响应体不可解析时引擎抛 AttributeError/TypeError/ValueError（视响应体形态），已由 `_ENGINE_PARSE_ERRORS` 归一到"响应畸形"（恢复旧文案与批量重试一次）。
- [空闲超时 60s 在极慢网关下误杀长静默但终将恢复的流] → 这正是裁定要的行为（60s 无 chunk 判挂）；前端有 15s 慢提示先于 60s 掐断告知用户；交互路径失败后用户手动重试成本可接受。
- [取消按钮的中断与失败收敛的边界（AbortError 误判为失败会触发自动重跑，违背取消语义）] → catch 分支显式区分 AbortError（决策 6-2）；测试/走查覆盖"取消后不自动重跑"。
- [本地开发模型切换（→deepseek/deepseek-v4.1-flash）烧真实额度] → 该模型为计费模型，且本地 `.env.dev` 实测 `AI_ENABLED=1`（非"缺省 0"）：手动触发搜索/翻译/补译等 AI 操作会真实烧额度（单次搜索几分钱量级）；自动消耗已被关闭——`RADAR_JOBS_ENABLED=0` 关掉进程内每日调度，故无后台自动调用。生产侧 AI 常开属预期形态。
- [streaming-search-progress 尚未归档，本 change 与其 delta 叠加于同一 spec 文件的相邻 Requirement] → 裁定 9 已钉归档顺序（本 change 在后）；validate/archive 时叠加态由顺序保证。

## Migration Plan

无数据迁移：无 schema 变更。部署＝依赖安装（`pip install` 新增 langchain-deepseek）＋代码上线重启服务；回滚＝回退 commit＋重装旧依赖重启（无持久状态）。配置侧：生产 `.env.prod` 零改动（模型已是 deepseek/deepseek-v4.1-flash、AI_TIMEOUT_SECONDS 已显式 300）；开发侧 `.env.dev` 改模型一行。部署后按 tasks.md 执行生产真实流式搜索留痕（逐帧到达时刻＋取消/慢提示走查）。

## Open Questions（存疑点登记，起草中冒出、未预案的判断点）

1. **批量重试的 1s 固定等待是否必须精确保留**（决策 2 末段）：with_retry 表达不出"固定 1s"，暂定接受零等待；若须保留则批量路径改薄包装。——待主 agent/本人拍板。
2. **取消按钮＋脉冲占位＋慢提示属新视觉元素**，项目 §2 示意图分档（有新交互模式→原型确认再开发）是否要求先出原型/示意图过本人？暂定按"既有冻结视觉体系（T041 进度面板）内的组件追加"处理、开发完真实页面截图交付过目；若裁定需原型先行，tasks 相应前置。——待主 agent 拍板。
