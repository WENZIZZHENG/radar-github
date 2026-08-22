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
系统 SHALL 把用户自然语言输入发给 DeepSeek，产出结构化查询：多个英文关键词、多个语言（取值对齐 classify.LANGUAGES 词表）、主题词、可选 filters 对象、可选 unsupported 数组，JSON 约定格式；关键词与语言均为数组，不得锁单个。LLM 返回非法 JSON 时 MUST 退化为以原输入为单一关键词的检索（不当场失败）。追问时系统 SHALL 把上一轮意图与新输入一并发给 DeepSeek 做意图合并，产出合并后的新意图（同一 JSON 契约，filters 一并合并：新输入的条件覆盖同名字段、未提及的保留）；合并调用返回非法 JSON 时 MUST 退化为以本轮新输入为单一关键词的检索（MUST NOT 沿用旧意图）。

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

### Requirement: 池内召回与精选
系统 SHALL 在 alive 仓内做关键词组匹配（`full_name`/`description_en`/`topics` 任一命中任一关键词计一次命中，语言过滤取交集），按命中数降序、最新星数降序粗排取约前 50 为候选，再交 DeepSeek 按匹配度精选至多 20 条并各生成一句推荐理由（≤50 字）；LLM 只许从候选清单中选择。精选失败（非法返回/超范围引用）MUST 退化为按粗排顺序直出池内候选（无推荐理由），记 WARNING。结果不足 20 条按实际展示，不凑数。意图含 filters 时召回 SHALL 叠加结构化过滤：`created_within_days` → `github_created_at` 非 NULL 且距今小于该天数（NULL 仓 MUST NOT 入选）；`min_stars` → 最新快照星数不低于该值。

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

