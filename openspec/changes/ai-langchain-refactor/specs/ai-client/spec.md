# Spec Delta：ai-client（ai-langchain-refactor，新建 capability）

## Purpose

AI 客户端是跨智能搜索/关注推荐/简介翻译/AI 概要/候选主题建议共享的基础设施：本规格钉死其唯一入口外壳、底层引擎选型约束、流式与思维链透出形态、超时与重试双口径、错误分类与对外异常契约，使内部实现（LangChain ChatDeepSeek）可替换而调用方与用户可见错误姿态稳定。

## ADDED Requirements

### Requirement: AI 调用唯一入口外壳
系统 SHALL 以 `DeepSeekClient` 为全仓唯一 AI 调用入口（智能搜索意图理解/精选、每周推荐理由、简介翻译、AI 概要、候选主题建议共享同一外壳类）；外壳公开方法签名（方法名、参数集、返回值契约、对外异常类型）MUST 保持稳定，内部引擎替换时调用方（检索链路/Web 路由/后台任务）零改动。API key 缺失时 MUST 在使用点拦截并抛账户类异常（config 层不报错，调用第一刀才报错——与 GitHubClient 同姿态）。

#### Scenario: 引擎替换调用方零改动
- **WHEN** AI 客户端内部实现由 openai SDK 直调替换为 LangChain ChatDeepSeek
- **THEN** `DeepSeekClient` 公开方法签名不变，search/routes/jobs 调用点代码零改动

#### Scenario: key 缺失使用点拦截
- **WHEN** AI_API_KEY 未配置（或 AI_ENABLED=0）
- **THEN** 首次 AI 调用即抛 DeepSeekAuthError（人话提示配置方法），榜单主链路不受影响

### Requirement: 底层引擎选型约束（LangChain ChatDeepSeek）
AI 客户端内部实现 SHALL 使用 `langchain-deepseek` 的 ChatDeepSeek（版本下界钉 spike 实测版 1.1.1）——选型硬约束为「思维链 `reasoning_content` 流式增量可透出」与「`response_format={"type":"json_object"}` 共存」同时满足（2026-10-13 spike 实证其为唯一满足的客户端，留痕 `data/ai-fw-spike/result.json`）。`langchain-openai` 的 ChatOpenAI MUST NOT 用作 AI 引擎（其按设计丢弃第三方 reasoning 字段，思维链场景不可用）。LangChain 模型实例构建 SHALL 收敛为单点 factory；系统 MUST NOT 引入 langgraph/agents 等编排组件。

#### Scenario: 思维链与结构化输出共存
- **WHEN** 流式调用带 `response_format={"type":"json_object"}`
- **THEN** 思维链增量与正文增量均可透出（spike 实证：流式 json_object 下 reasoning_chars=49、正文 JSON 合法）

#### Scenario: 不可替代客户端
- **WHEN** 评估以 ChatOpenAI 替换 ChatDeepSeek
- **THEN** 拒绝——ChatOpenAI 拿不到 reasoning 字段（spike 实证 case1 reasoning_chars=0），思维链显示能力丧失

### Requirement: 流式调用与思维链透出
流式调用 SHALL 逐 chunk 从 `additional_kwargs["reasoning_content"]`（字符串增量）取思维链增量并下发思维链回调；正文 content 增量 SHALL 只累积、末尾一次性解析（"只搬文本增量、末尾一次性解析"形态不变）。思维链字段 SHALL 只认 `reasoning_content`——MUST NOT 保留 `reasoning_details` 数组形态的兼容代码（ChatDeepSeek 不解析该字段，spike 实证免费模型下 0 透出，兼容分支属死代码）。模型响应不含 `reasoning_content` 时 MUST 退化为零思维链增量、不报错（调用方退化为纯阶段进度）。思维链 MUST 如实透传原文，不翻译、不承诺中文。

#### Scenario: 思维链增量逐段下发
- **WHEN** 模型流式响应携带 reasoning_content 增量
- **THEN** 每个增量如实喂给思维链回调，正文增量累积至流结束一次性返回解析

#### Scenario: 无思维链字段退化
- **WHEN** 模型流式响应不含 reasoning_content（或为空）
- **THEN** 全程零思维链回调、不报错，正文照常累积返回

### Requirement: 超时口径（空闲超时＋连接总上限）
流式调用 SHALL 启用"60 秒无 chunk 即判挂"的空闲超时（映射引擎的 `stream_chunk_timeout` 类参数，其缺省 120s MUST NOT 沿用）；空闲超时触发（StreamChunkTimeoutError）MUST 归入超时类错误链分支。单次调用连接总上限 SHALL 为 300 秒防挂死（`AI_TIMEOUT_SECONDS` 环境变量可覆盖，缺省 300）。

#### Scenario: 上游静默掐断
- **WHEN** 流式进行中超过 60 秒无任何 chunk 到达
- **THEN** 调用判挂并按超时类错误处理（交互路径如实失败提示，批量路径重试一次）

#### Scenario: 连接总上限兜底
- **WHEN** 单次调用总时长达到 300 秒
- **THEN** 调用掐断并按超时类错误处理，不发生无限挂死

### Requirement: 重试双口径（交互不重试／批量重试一次）
交互路径（智能搜索的意图理解/意图合并/精选调用，流式形态与同步降级形态同口径）MUST NOT 自动重试——调用失败即抛对外异常，失败如实提示由用户手动重试。后台批量路径（每周推荐理由、简介翻译、AI 概要、候选主题建议、补译补齐）SHALL 保留"失败重试 1 次"口径，且仅对可重试类错误（超时/连接/5xx/响应畸形）重试；429 限速与其余 4xx MUST NOT 重试（盲重试只会放大调用量），账户类错误直通不重试。引擎与底层 SDK 的内置重试 MUST 全部关闭（避免与外壳重试叠加放大调用量），重试语义由外壳显式表达（如 with_retry 显式声明可重试异常白名单与次数）。

#### Scenario: 交互路径不自动重试
- **WHEN** 搜索的两段 LLM 调用任一失败（超时/5xx/连接错误）
- **THEN** 系统不发起第二次调用，按既有降级语义如实处理（意图段失败 → "搜索暂不可用"；精选段失败 → 退化粗排直出）

#### Scenario: 批量路径 5xx 重试一次
- **WHEN** 每周推荐理由生成中某次调用遇 HTTP 5xx
- **THEN** 等待后重试 1 次，再失败抛 DeepSeekError 跳过该条不阻断整轮

#### Scenario: 429 不重试
- **WHEN** 任一路径调用遇 HTTP 429 限速
- **THEN** 不重试，直接抛 DeepSeekError（既有姿态：防盲重试放大调用量）

### Requirement: 错误分类与对外异常契约
对外异常类型 SHALL 保持 `DeepSeekError`/`DeepSeekAuthError` 两类不变，人话文案逐字不变——唯一例外：交互路径不再自动重试，"重试 N 次后放弃"类文案在交互路径消失（刻意行为变化，见 smart-search 修订）。错误分类 SHALL 维持六分支判级：401 → DeepSeekAuthError 直通；403 命中 content_policy → DeepSeekError（单条失败跳过），其余 403 → DeepSeekAuthError；402 → DeepSeekAuthError；429 及其余 4xx → DeepSeekError 不重试；5xx/超时（含空闲超时）/连接错误/响应畸形 → 批量路径重试一次后抛 DeepSeekError、交互路径直接抛。引擎封装异常（继承 openai SDK 对应基类）SHALL 复用同一分类链；`StreamChunkTimeoutError`（不在 openai 异常体系内）SHALL 显式映射入超时分支。

#### Scenario: 鉴权错误直通
- **WHEN** 调用返回 HTTP 401
- **THEN** 抛 DeepSeekAuthError（提示更新 AI_API_KEY），不重试

#### Scenario: 内容审核拦截
- **WHEN** 调用返回含 content_policy 标记的 HTTP 403
- **THEN** 抛 DeepSeekError 作单条失败处理（批量路径跳过该条），不归入账户类

#### Scenario: 空闲超时归超时分支
- **WHEN** 流式空闲超时触发 StreamChunkTimeoutError
- **THEN** 按超时类错误链处理（批量重试一次／交互直接失败），不被误判为"未知异常"

### Requirement: 结构化输出协议约束
JSON 产出调用（意图理解/意图合并/精选/候选主题建议）请求 SHALL 带 `response_format={"type":"json_object"}` 作为协议层 JSON 约束（第一道防线；生产实测提速 22.7s→3.8s，2026-10-13 留痕）；手搓容错解析（剥 markdown 围栏＋大括号截取）SHALL 保留为第二道防线，防网关/模型行为差异。端点对该参数报 400 时，系统 MUST NOT 做"去掉参数重发"的自动回退，按错误链处理。纯文本产出方法（翻译/推荐理由/概要）请求 MUST NOT 带 `response_format` 字段。

#### Scenario: 协议层与容错层双防线
- **WHEN** JSON 产出方法发起调用
- **THEN** 请求带 json_object 约束；返回内容仍经容错解析后才进入使用

#### Scenario: 端点拒绝参数不回退
- **WHEN** 端点对 `response_format` 参数报 400
- **THEN** 系统不去掉参数自动重发，按错误链处理

### Requirement: 配置与开发模型口径
AI 连接配置 SHALL 走环境变量（AI_API_KEY/AI_BASE_URL/AI_MODEL/AI_TIMEOUT_SECONDS）；开发环境默认 AI_MODEL SHALL 为 `deepseek/deepseek-v4.1-flash`——`stealth/glyph-cluster:free` 类免费模型 MUST NOT 用作开发默认模型（spike 实证：对结构化输出报 HTTP 400 且思维链走 reasoning_details 数组不透出，本实现下不可用）。模型推理档位由提供方账户侧管理，不在本规格配置面（不许以"降档"作为性能手段）。

#### Scenario: 开发默认模型可用
- **WHEN** 本地开发按 .env.example/.env.dev 缺省模型发起搜索
- **THEN** 结构化输出与思维链流式透出均正常工作（spike 实证组合可用）
