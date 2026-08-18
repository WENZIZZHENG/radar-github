# 规格：智能搜索（smart-search）

## ADDED Requirements

### Requirement: 搜索入口与页面
系统 SHALL 提供独立搜索页并在全站导航栏加入口。页面包含一个自然语言输入框与结果列表；结果行 MUST 复用榜单行组件（`_row.html`）及详情面板体系，每行附一句中文推荐理由（LLM 精选成功时）；页面 SHALL 把 LLM 解析出的关键词/语言透明显示（"理解为：…"形态）。

#### Scenario: 打开搜索页
- **WHEN** 用户点击导航栏"搜索"入口
- **THEN** 展示搜索页：输入框 + 提交按钮，无结果区（未搜索时）

#### Scenario: 意图透明
- **WHEN** 一次搜索完成
- **THEN** 结果区顶部展示 LLM 解析出的关键词与语言（如"理解为：crawler, scraping / Python"）

### Requirement: LLM 意图理解
系统 SHALL 把用户自然语言输入发给 DeepSeek，产出结构化查询：多个英文关键词、多个语言（取值对齐 classify.LANGUAGES 词表）、主题词，JSON 约定格式；关键词与语言均为数组，不得锁单个。LLM 返回非法 JSON 时 MUST 退化为以原输入为单一关键词的检索（不当场失败）。

#### Scenario: 多关键词多语言扩展
- **WHEN** 用户输入"我要做爬虫"
- **THEN** LLM 产出多个关键词（如 crawler/scraping/spider/web-scraping 之属）与语言数组（如 ["Python"]）

#### Scenario: 非法 JSON 退化
- **WHEN** LLM 意图理解返回无法解析的内容
- **THEN** 系统以用户原输入作为单一关键词继续池内检索，不报错页

### Requirement: 池内召回与精选
系统 SHALL 在 alive 仓内做关键词组匹配（`full_name`/`description_en`/`topics` 任一命中任一关键词计一次命中，语言过滤取交集），按命中数降序、最新星数降序粗排取约前 30 为候选，再交 DeepSeek 按匹配度精选至多 10 条并各生成一句推荐理由（≤50 字）；LLM 只许从候选清单中选择。精选失败（非法返回/超范围引用）MUST 退化为按粗排顺序直出池内候选（无推荐理由），记 WARNING。结果不足 10 条按实际展示，不凑数。

#### Scenario: 正常精选
- **WHEN** 池内候选 30 条交 LLM 精选
- **THEN** 返回按匹配度排序的至多 10 条，每条带一句中文推荐理由

#### Scenario: 精选失败兜底
- **WHEN** LLM 精选返回非法结果
- **THEN** 按粗排顺序直出池内候选（至多 10 条），行无推荐理由，日志记 WARNING

#### Scenario: 池内不足 10 条
- **WHEN** 池内候选只有 4 条
- **THEN** 展示 4 条池内结果，并触发池外补搜

### Requirement: 池外实时补搜
池内结果不足 10 条时，系统 SHALL 用同组关键词（含语言限定）实时调 GitHub Search API 补足至 10 条。池外结果 MUST 带"池外"徽标、展示 GitHub 原始描述、无增星/译文/概要字段，排序永远在池内结果之后。Search 失败/限速/token 缺失时 MUST 跳过补搜：池内结果照常展示，补搜区如实标注失败或不出现。

#### Scenario: 池外补足
- **WHEN** 池内结果 4 条
- **THEN** 池外补搜追加至多 6 条，带"池外"徽标，排在池内 4 条之后

#### Scenario: 补搜失败
- **WHEN** GitHub Search 调用失败或限速
- **THEN** 只展示池内结果，池外区标注"补搜失败"或不渲染，页面不报错

### Requirement: 空结果如实说明
池内与池外均无结果时，系统 MUST NOT 编造推荐，SHALL 如实展示说明（含已理解的关键词/语言，便于用户修正输入）。

#### Scenario: 全空
- **WHEN** 池内零候选且池外补搜零结果或不可用
- **THEN** 页面展示"没有找到匹配项目"形态的说明与已理解的查询词，无任何编造条目

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
