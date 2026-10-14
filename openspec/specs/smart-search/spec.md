# 规格：智能搜索（smart-search）

## Purpose

提供自然语言智能搜索页（P8）：LLM 意图理解 → 池内召回精选 → 池外实时补搜 → 池外一键关注入池。本规格由 change `smart-search` 归档沉淀（2026-08-19）。
## Requirements
### Requirement: 搜索入口与页面
系统 SHALL 提供独立搜索页并在全站导航栏加入口。页面包含一个自然语言输入框与结果列表；结果行 MUST 复用榜单行组件（`_row.html`）及详情面板体系，每行附一句中文推荐理由（LLM 精选成功时）；页面 SHALL 把 LLM 解析出的关键词/语言透明显示（"理解为：…"形态）。首搜与追问 SHALL 复用结果页顶部同一输入框（无单独追问框、无轮次历史记录）；追问后意图透明行 SHALL 显示当前生效的合并意图。意图透明行 SHALL 同步展示生效的结构化筛选条件（filters，如"创建：近 1 年内"／"星数 ≥5000"形态）与未支持条件回显（`unsupported` 非空时展示"暂不支持：…"段，仅展示不影响检索）。

#### Scenario: 打开搜索页
- **WHEN** 用户点击导航栏"搜索"入口
- **THEN** 展示搜索页：输入框 + 提交按钮，无结果区（未搜索时）

#### Scenario: 意图透明
- **WHEN** 一次搜索完成
- **THEN** 结果区顶部展示 LLM 解析出的关键词与语言（如"理解为：crawler, scraping / Python"）

#### Scenario: 追问后意图行更新
- **WHEN** 一次追问搜索完成
- **THEN** 意图透明行展示合并后的新意图（而非本轮原始输入）

#### Scenario: 筛选条件与不支持回显
- **WHEN** 意图含 `created_within_days=365` 与 `unsupported=["最近一周有提交"]`
- **THEN** 意图透明行展示筛选条件段（如"创建：近 1 年内"）与"暂不支持：最近一周有提交"，后者不影响检索结果

### Requirement: LLM 意图理解
系统 SHALL 把用户自然语言输入发给 DeepSeek，产出结构化查询：多个英文关键词、多个语言（取值对齐 classify.LANGUAGES 词表）、主题词、可选 filters 对象、可选 unsupported 数组，JSON 约定格式；关键词与语言均为数组，不得锁单个。LLM 返回非法 JSON 时 MUST 退化为以原输入为单一关键词的检索（不当场失败）。追问时系统 SHALL 把上一轮意图与新输入一并发给 DeepSeek 做意图合并，产出合并后的新意图（同一 JSON 契约，filters 一并合并：新输入的条件覆盖同名字段、未提及的保留）；合并调用返回非法 JSON 时 MUST 退化为以本轮新输入为单一关键词的检索（MUST NOT 沿用旧意图）。意图理解与意图合并调用请求 SHALL 带 `response_format={"type":"json_object"}` 作为协议层 JSON 约束（第一道防线）；既有手搓容错解析（剥 markdown 围栏＋大括号截取）SHALL 保留为第二道防线，防网关/模型行为差异。端点对 `response_format` 参数报 400 时，系统 MUST NOT 做"去掉参数重发"的自动回退，按错误链处理（生产网关已实测支持该参数，2026-10-12 留痕）。意图理解与意图合并属交互路径：调用失败（超时/连接/5xx 等）MUST NOT 自动重试，失败如实提示由用户手动重试——"重试 N 次后放弃"类文案在本路径消失（刻意行为变化，2026-10-13 拍板）。

#### Scenario: 多关键词多语言扩展
- **WHEN** 用户输入"我要做爬虫"
- **THEN** LLM 产出多个关键词（如 crawler/scraping/spider/web-scraping 之属）与语言数组（如 ["Python"]）

#### Scenario: 非法 JSON 退化
- **WHEN** LLM 意图理解返回无法解析的内容
- **THEN** 系统以用户原输入作为单一关键词继续池内检索，不报错页

#### Scenario: 追问意图合并
- **WHEN** 上一轮意图为关键词 [crawler, scraping]＋语言 [Python]，本轮输入"换成 Go 的"
- **THEN** LLM 产出合并后新意图（语言替换为 Go，关键词按需保留/调整），系统以新意图重跑检索链路

#### Scenario: 合并返回非法 JSON
- **WHEN** 追问的意图合并调用返回无法解析的内容
- **THEN** 系统以本轮新输入作为单一关键词检索（不沿用旧意图），不报错页

#### Scenario: 时间条件入 filters
- **WHEN** 用户输入"我要做爬虫，要创建时间 1 年内的"
- **THEN** LLM 产出 filters.created_within_days=365（条件 MUST NOT 出现在关键词里）

#### Scenario: 协议层 JSON 约束
- **WHEN** 系统发起意图理解或意图合并调用
- **THEN** 请求带 `response_format={"type":"json_object"}`，返回内容仍经既有容错解析（第二道防线）后才进入意图使用

#### Scenario: 端点拒绝参数不自动回退
- **WHEN** 端点对 `response_format` 参数报 400
- **THEN** 系统不去掉参数自动重发，按既有错误链处理（调用失败 → 搜索页"暂不可用"/降级语义不变）

#### Scenario: 交互路径失败不自动重试
- **WHEN** 意图理解或意图合并调用因超时/连接错误/5xx 失败
- **THEN** 系统不发起第二次调用，搜索页如实展示"搜索暂不可用"提示，由用户手动重试

### Requirement: 池内召回与精选
系统 SHALL 在 alive 仓内做关键词组匹配（`full_name`/`description_en`/`topics` 任一命中任一关键词计一次命中，语言过滤取交集），按命中数降序、最新星数降序粗排取约前 50 为候选，再交 DeepSeek 按匹配度精选至多 20 条并各生成一句推荐理由（≤50 字）；LLM 只许从候选清单中选择。精选失败（非法返回/超范围引用）MUST 退化为按粗排顺序直出池内候选（无推荐理由），记 WARNING。结果不足 20 条按实际展示，不凑数。意图含 filters 时召回 SHALL 叠加结构化过滤：`created_within_days` → `github_created_at` 非 NULL 且距今小于该天数（NULL 仓 MUST NOT 入选）；`min_stars` → 最新快照星数不低于该值。精选调用请求 SHALL 带 `response_format={"type":"json_object"}`（协议层第一道防线），既有手搓容错解析 SHALL 保留为第二道防线；端点对该参数报 400 时 MUST NOT 去掉参数自动重发，按错误链处理。精选属交互路径：调用失败（超时/连接/5xx 等）MUST NOT 自动重试，直接按精选失败语义退化（粗排直出）——不再"先自动重试一次再退化"（刻意行为变化，2026-10-13 拍板）。

#### Scenario: 正常精选
- **WHEN** 池内候选 50 条交 LLM 精选
- **THEN** 返回按匹配度排序的至多 20 条，每条带一句中文推荐理由

#### Scenario: 精选失败兜底
- **WHEN** LLM 精选返回非法结果
- **THEN** 按粗排顺序直出池内候选（至多 20 条），行无推荐理由，日志记 WARNING

#### Scenario: 池内不足 20 条
- **WHEN** 池内候选只有 4 条
- **THEN** 展示 4 条池内结果，并触发池外补搜

#### Scenario: 创建时间过滤
- **WHEN** 意图含 filters.created_within_days=365
- **THEN** 召回结果只含 github_created_at 距今 <365 天的仓；github_created_at 为 NULL 的仓不入选

#### Scenario: 星数下限过滤
- **WHEN** 意图含 filters.min_stars=5000
- **THEN** 召回结果只含最新快照星数 ≥5000 的仓

#### Scenario: 精选请求带协议层约束
- **WHEN** 系统发起精选调用
- **THEN** 请求带 `response_format={"type":"json_object"}`，返回内容仍经既有容错解析后才用于精选结果

#### Scenario: 精选端点拒绝参数不自动回退
- **WHEN** 精选调用因 `response_format` 参数被端点 400 拒绝
- **THEN** 系统不去掉参数自动重发，按既有错误链处理（退化为粗排直出或不可用提示，语义不变）

#### Scenario: 精选调用失败不重试
- **WHEN** 精选调用因超时/连接错误/5xx 失败
- **THEN** 系统不发起第二次精选调用，直接退化为按粗排顺序直出池内候选（退化语义不变，仅不再先自动重试）

### Requirement: 池外实时补搜
池内结果不足 20 条时，系统 SHALL 用同组关键词（含语言限定）实时调 GitHub Search API 补足至 20 条；意图含 filters 时查询 SHALL 追加对应限定词（`created_within_days` → `created:>YYYY-MM-DD`；`min_stars` → `stars:>=N`——限定词不占单查询 5 个布尔运算符预算，关键词截前 4、语言截前 2 的口径不变）。池外结果 MUST 带"池外"徽标、展示 GitHub 原始描述、无增星/译文/概要字段，排序永远在池内结果之后。Search 失败/限速/token 缺失时 MUST 跳过补搜：池内结果照常展示，补搜区如实标注失败或不出现。

#### Scenario: 池外补足
- **WHEN** 池内结果 4 条
- **THEN** 池外补搜追加至多 16 条，带"池外"徽标，排在池内 4 条之后

#### Scenario: 补搜失败
- **WHEN** GitHub Search 调用失败或限速
- **THEN** 只展示池内结果，池外区标注"补搜失败"或不渲染，页面不报错

#### Scenario: 带筛选条件的补搜
- **WHEN** 意图含 filters.created_within_days=365 且需补搜
- **THEN** GitHub Search 查询含 `created:>` 限定词（日期由当前时间回推 365 天），补搜结果同为创建未满 1 年的仓

### Requirement: 空结果如实说明
池内与池外均无结果时，系统 MUST NOT 编造推荐，SHALL 如实展示说明（含已理解的关键词/语言，便于用户修正输入）。追问搜空时系统 SHALL 额外展示新旧意图对比，并用随表单回传的上一轮结果摘要原样重渲染上一轮结果区（标注为上一轮结果），MUST NOT 为此重跑旧意图检索或额外调用 LLM；上一轮结果摘要缺失/非法时退化为仅展示空结果说明。

#### Scenario: 全空（首搜）
- **WHEN** 池内零候选且池外补搜零结果或不可用
- **THEN** 页面展示"没有找到匹配项目"形态的说明与已理解的查询词，无任何编造条目

#### Scenario: 追问搜空保留上一轮
- **WHEN** 追问检索池内＋池外全空，且回传的上一轮结果摘要有效
- **THEN** 页面顶部展示空结果说明与新旧意图对比，上一轮结果区原样保留在下方，不发生额外 LLM 调用

### Requirement: 池外结果一键关注入池
池外结果行 SHALL 提供关注入口，点击后走既有 `ingest_followed_repo` 动态入池（source='follow' + 基线快照）；已在池/已关注的池外行 MUST NOT 显示关注入口（按 full_name 查库判定）。

#### Scenario: 关注池外仓
- **WHEN** 用户点击某池外结果的关注星
- **THEN** 该仓经 `ingest_followed_repo` 入池，行上关注星变为已关注态；次日起进入采集与翻译队列

#### Scenario: 已在池不重复关注
- **WHEN** 池外结果中某仓实际已在池（同名）
- **THEN** 该行不显示关注入口

### Requirement: AI 降级姿态
DeepSeek key 未配置/鉴权失败/调用失败时，搜索页 SHALL 展示"搜索暂不可用"的明确提示；榜单等主链路 MUST NOT 受任何影响。任何 AI 异常 MUST NOT 中断快照主流程（延伸 ai.py 既有姿态）。

#### Scenario: DeepSeek 不可用
- **WHEN** DEEPSEEK_API_KEY 未配置或调用鉴权失败
- **THEN** 搜索页展示明确的不可用提示，榜单页与每日采集照常

### Requirement: 结构化筛选条件白名单（filters）
意图契约 SHALL 含可选 filters 对象，字段仅限服务端白名单——第一版为 `created_within_days`（int，创建至今天数上限）与 `min_stars`（int，最新星数下限）；缺省/空 = 不过滤。服务端 MUST 逐字段校验白名单与类型（白名单外或类型非法的 filters 字段 MUST 丢弃并记 WARNING，不当场失败）；池内召回与池外补搜 SHALL 按白名单映射过滤（见各 Requirement）。LLM 只许产出白名单内 filters 字段；白名单外的条件 MUST 放入仅展示的 `unsupported` 字符串数组（不影响检索），MUST NOT 塞进关键词。

#### Scenario: 白名单外字段被丢弃
- **WHEN** LLM 产出 filters 含白名单外字段（如 `forks_min`）或类型非法（如 `created_within_days="一年"`）
- **THEN** 服务端丢弃该字段并记 WARNING，其余合法 filters 照常生效

#### Scenario: 不支持条件仅回显
- **WHEN** 用户输入含池内无数据支撑的条件（如"最近一周有提交"——未采集 pushed_at）
- **THEN** LLM 将其放入 unsupported，检索不受影响，意图透明行如实展示"暂不支持：最近一周有提交"

#### Scenario: 追问中 filters 合并
- **WHEN** 上一轮意图 filters.created_within_days=365，本轮输入"放宽到 3 年内"
- **THEN** 合并后 filters.created_within_days 更新为约 1095（同名字段覆盖），其余 filters 未提及则保留

### Requirement: 追问上下文无状态回传
追问上下文 SHALL 无状态：服务端 MUST NOT 建会话表或落库搜索历史；每轮追问由前端把上一轮意图（JSON，含 filters/unsupported）随新输入经表单隐藏字段回传，服务端仅做"旧意图＋新输入 → LLM 合并 → 新意图 → 重跑检索"；刷新页面状态即清零。上一轮意图回传缺失、JSON 非法或字段形态非法时，系统 MUST 丢弃并按首搜处理，MUST NOT 报错页。上一轮结果摘要同样经隐藏字段回传，仅用于追问搜空时的重渲染（模板转义照常）。

#### Scenario: 正常追问回传
- **WHEN** 结果页提交追问（隐藏字段携带上一轮意图 JSON 与结果摘要）
- **THEN** 服务端解析旧意图并进入意图合并链路，全程无服务端会话状态

#### Scenario: 回传非法按首搜
- **WHEN** 追问请求中 prev_intent 缺失或无法解析
- **THEN** 系统按首搜处理本轮输入（无意图合并），页面正常渲染不报错

### Requirement: 搜索流式加载态（分阶段进度）
搜索执行等待期间，系统 SHALL 经流式端点（`POST /search/stream`）按检索链路阶段实时下发进度事件：理解意图 → 池内召回 → AI 精选 → 补搜 GitHub；页面 SHALL 展示阶段步骤条（未开始/进行中/已完成三态如实），进行中步骤 SHALL 显示已用时秒数（每秒跳动）作为上游静默期的存活信号（纯前端计时，不改事件协议；步骤进入终态即停表消失）。池内精选后不足 20 条才进入补搜阶段；无需补搜时补搜阶段 SHALL 如实标注跳过（不出现假装执行的进度）。进度事件 MUST NOT 改变检索链路本身的语义（降级/退化口径与同步路径一致）。搜索进行中页面 SHALL 提供「取消」按钮：点击后中断流式请求（AbortController），进度区如实提示已取消并恢复可提交态；取消是用户主动行为而非失败，MUST NOT 触发失败收敛的自动重跑。进行中步骤超过 15 秒无任何新增量（阶段事件与思维链增量均无）时，页面 SHALL 在步骤条下升级提示"网关响应较慢，仍在等待"；新增量到达或步骤进入终态后提示消退。

#### Scenario: 阶段逐步推进
- **WHEN** 用户提交搜索且浏览器支持流式消费
- **THEN** 页面步骤条随服务端事件逐阶段点亮：理解意图（进行中）→ 完成 → 池内召回 → AI 精选 → 补搜 GitHub 或标注"无需补搜"；进行中步骤显示已用时秒数且每秒跳动，进入终态后计时消失

#### Scenario: 无需补搜如实标注
- **WHEN** 池内精选结果已达 20 条
- **THEN** 补搜阶段显示"跳过（池内已足 20 条）"形态而非进行中动画

#### Scenario: 取消中断不自动重跑
- **WHEN** 搜索进行中用户点击「取消」按钮
- **THEN** 流式请求被中断，页面提示已取消并恢复可提交态，不发生自动重跑、不展示半成品结果

#### Scenario: 静默超 15 秒升级提示
- **WHEN** 进行中步骤超过 15 秒无任何新增量（上游静默）
- **THEN** 步骤条下显示"网关响应较慢，仍在等待"提示；新增量到达或步骤收尾后提示消退

### Requirement: AI 思维链流式显示与退化
两段 LLM 调用（意图理解/精选）等待期间，系统 SHALL 把模型思维链增量流式下发并在"AI 思考过程"区追加显示英文原文（两段调用分段显示，完成后可折叠）。思维链字段解析 SHALL 只认 `reasoning_content`（字符串增量）——`reasoning_details` 数组形态的兼容 MUST 退役（底层引擎 ChatDeepSeek 不解析该字段，spike 实证该形态下 0 透出，兼容分支属死代码）；字段缺或为空时 MUST 退化为纯阶段进度（不报错）。每段调用进入等待期（首条思维链增量到达前）时，"AI 思考过程"区 SHALL 立即渲染该段并显示"AI 思考中…"脉冲占位（扭转"首条增量才建思考区"的盲等体验）；首条增量到达后占位被原文流式追加替换；整段调用结束仍无任何增量时占位随段收尾移除，不保留空思考区（退化口径不破）。思维链 MUST 如实显示原文，系统不翻译、不承诺中文。

#### Scenario: 思维链分段流式追加
- **WHEN** 意图理解调用返回 reasoning 增量
- **THEN** "AI 思考过程"区意图理解段实时追加英文原文；精选调用开始时新增精选段继续追加

#### Scenario: 等待期脉冲占位
- **WHEN** 一段 LLM 调用已发起、首条思维链增量尚未到达
- **THEN** "AI 思考过程"区立即显示该段"AI 思考中…"脉冲占位，不等到首条增量才建区

#### Scenario: 占位被增量替换
- **WHEN** 首条思维链增量到达
- **THEN** 脉冲占位被原文流式追加替换，用户看到思维链从等待期无缝过渡到实时内容

#### Scenario: 无思维链退化
- **WHEN** 一段调用结束仍不含 reasoning 增量（或字段为空）
- **THEN** 占位随段收尾移除、不保留空思考区，搜索照常完成（纯阶段进度）

### Requirement: 流式与同步路径语义唯一与降级收敛
系统 SHALL 保留既有同步 `POST /search` SSR 路径；流式路径与同步路径 MUST 跑同一检索链路、渲染同一结果区模板（结果行复用 `_row.html`），两条路径的搜索结果内容/形态一致，仅加载态不同。流式结束 MUST 下发 SSR 渲染好的结果区 HTML 片段（含意图透明行、结果区、补搜标注、追问对比等既有元素），前端整体替换结果容器并更新追问隐藏字段（prev_intent/prev_results，无状态回传口径不变）。浏览器不支持流式消费（无 JS/无 ReadableStream）或流中途失败时，系统 MUST 收敛回同步表单提交路径，不产生第三条结果形态。

#### Scenario: 两路径结果一致
- **WHEN** 同一查询分别经流式端点与同步 POST 执行
- **THEN** 两条路径的结果区内容一致（同链路同模板），仅加载过程不同

#### Scenario: 无 JS 降级
- **WHEN** 浏览器禁用 JS 或不支持流式消费
- **THEN** 表单原生提交走同步 POST 路径，SSR 结果页正常渲染（现状行为不变）

#### Scenario: 流中途失败收敛
- **WHEN** 流式请求在结果事件到达前中断（网络异常/服务端错误）
- **THEN** 页面如实提示失败并以同步表单提交重跑（结果语义不变），不显示半成品结果区
