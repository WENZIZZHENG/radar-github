# 规格增量：AI JSON 产出调用启用 response_format（ai-json-response-format → smart-search）

本增量只改变现容错层级（协议层 `json_object` 约束为第一道、手搓容错解析保留为第二道）与端点拒绝参数时的姿态；对外降级口径（非法 JSON 退化、精选失败兜底）一字不变，页面/文案/交互零变化。`suggest_topics` 无 spec 级解析契约（规格自查见 proposal），其请求参数变化不进本 delta。

## MODIFIED Requirements

### Requirement: LLM 意图理解
系统 SHALL 把用户自然语言输入发给 DeepSeek，产出结构化查询：多个英文关键词、多个语言（取值对齐 classify.LANGUAGES 词表）、主题词、可选 filters 对象、可选 unsupported 数组，JSON 约定格式；关键词与语言均为数组，不得锁单个。LLM 返回非法 JSON 时 MUST 退化为以原输入为单一关键词的检索（不当场失败）。追问时系统 SHALL 把上一轮意图与新输入一并发给 DeepSeek 做意图合并，产出合并后的新意图（同一 JSON 契约，filters 一并合并：新输入的条件覆盖同名字段、未提及的保留）；合并调用返回非法 JSON 时 MUST 退化为以本轮新输入为单一关键词的检索（MUST NOT 沿用旧意图）。意图理解与意图合并调用请求 SHALL 带 `response_format={"type":"json_object"}` 作为协议层 JSON 约束（第一道防线）；既有手搓容错解析（剥 markdown 围栏＋大括号截取）SHALL 保留为第二道防线，防网关/模型行为差异。端点对 `response_format` 参数报 400 时，系统 MUST NOT 做"去掉参数重发"的自动回退，按既有错误链处理（走重试→降级/报错；生产网关已实测支持该参数，2026-10-12 留痕）。

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

### Requirement: 池内召回与精选
系统 SHALL 在 alive 仓内做关键词组匹配（`full_name`/`description_en`/`topics` 任一命中任一关键词计一次命中，语言过滤取交集），按命中数降序、最新星数降序粗排取约前 50 为候选，再交 DeepSeek 按匹配度精选至多 20 条并各生成一句推荐理由（≤50 字）；LLM 只许从候选清单中选择。精选失败（非法返回/超范围引用）MUST 退化为按粗排顺序直出池内候选（无推荐理由），记 WARNING。结果不足 20 条按实际展示，不凑数。意图含 filters 时召回 SHALL 叠加结构化过滤：`created_within_days` → `github_created_at` 非 NULL 且距今小于该天数（NULL 仓 MUST NOT 入选）；`min_stars` → 最新快照星数不低于该值。精选调用请求 SHALL 带 `response_format={"type":"json_object"}`（协议层第一道防线），既有手搓容错解析 SHALL 保留为第二道防线；端点对该参数报 400 时 MUST NOT 去掉参数自动重发，按既有错误链处理。

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
