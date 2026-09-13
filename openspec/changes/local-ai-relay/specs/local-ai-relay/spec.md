# 规格：本地 AI 导出/回填通道（local-ai-relay）

## ADDED Requirements

### Requirement: 待生成任务的导出判定与每日 ensure 同口径

系统 SHALL 提供待生成任务导出接口 `GET /api/local-ai/tasks`，其任务判定与每日 AI 段（`ensure_daily_ai` → `recommend_missing`）逐条同口径，覆盖集 S = 三口径榜去重 ∪ 关注集（复用 `_scope_sets`）：

- `translate`：S 内 `description_zh IS NULL` 且英文简介非空且不含 CJK 的仓库；
- `week`：缺 `(repo_id, 'week', 当周标签)` 行的仓库；
- `quarter`：缺当季行，或跑批日在半月窗口内且既有行 `generated_week ≠ 当周` 的仓库；
- `total`：缺行，或跑批日在半月窗口内且本次拉到 README blob sha 与库内指纹不同的仓库；
- `summary`：同 `total` 段口径。

已被标记 `source='manual'` 的行 MUST NOT 作为重生任务导出（缺失补缺不受影响）。接口只读：不写库、不占运行锁、不影响每日跑批。

#### Scenario: 非窗口日只导出缺失任务
- **GIVEN** 当日不是 1 号也不是 15 号，某仓库已有当周 week 行
- **WHEN** 调用导出接口
- **THEN** 该仓库不出现 week 任务；同一仓库缺 summary 行时仍出现 summary 任务

#### Scenario: 窗口日导出重生任务
- **GIVEN** 当日为窗口日，某仓库已有人工写入的 total 行（`source='manual'`）与另一仓库的 ai 行 README sha 已变化
- **WHEN** 调用导出接口
- **THEN** 人工行仓库不产生 total 任务；ai 行仓库产生 total 任务

#### Scenario: 超出单批上限
- **GIVEN** 当前判定下待生成任务总数大于请求的 `limit`
- **WHEN** 调用导出接口
- **THEN** 返回恰好 `limit` 条任务，并在响应中给出剩余待办总数（`remaining`）

#### Scenario: 导出被 kind 过滤
- **WHEN** 请求指定 `kind=week`
- **THEN** 仅返回 week 类任务，其他类别不出现

### Requirement: 导出内容包含完整 prompt 与作业单

导出接口 SHALL 为每条任务给出直接可用的 `system` 与 `user` prompt 全文（README 按 `AI_README_HEAD_CHARS` 截断后并入 user；README 拉取失败/为空时退化为元数据输入，不阻塞导出）。`translate` 类任务 SHALL 额外附带 `src`＝该仓库当前 `description_en` 的 sha1 前 8 位（原文指纹，供回填时校验"译的是哪份原文"），其余类别 MUST NOT 携带该字段。`format=text` 时 SHALL 输出一份自包含作业单：任务说明 + 输出格式要求（JSON 对象，每条含 `repo`、`kind`、`period_label`、`text`，`translate` 另含 `src`）+ 逐条任务块；`format=json` 时 SHALL 输出等价的结构化清单，并附 `as_of`、`week_label`、`quarter_label`、`window_open`、`remaining`、`probe_skipped`、`probe_truncated`。窗口日的 README 探测预算 SHALL 由 total 与 summary 两段各自持有（共用会导致先跑的段吃光预算、后段整轮零判定），未判定候选数由 `probe_skipped` 报出；未判定 SHALL 覆盖两种原因——探测预算耗尽、以及本批条数已凑满（`limit`）而未跑到的段尾候选。`probe_skipped` SHALL 被表述为**告警值而非进度**：探测无服务端状态，同一窗口日重复导出不会使其清零、数值不代表剩余工作量；窗口日的收工判据是"本次导出没有任何 total/summary 重生任务"。两种格式的任务集合 MUST 一致。

#### Scenario: 作业单自带输出格式要求
- **WHEN** 以 `format=text` 导出
- **THEN** 作业单文本中包含对本地工具的 JSON 输出格式要求（含四个字段名；`translate` 条目另要求原样回带 `src`），可直接整段粘贴给本地工具

#### Scenario: README 拉取失败退化
- **GIVEN** 某仓库 README 拉取失败
- **WHEN** 导出该仓库任务
- **THEN** 该条任务仍被导出，user prompt 只含元数据、不含 README 段落

### Requirement: 回填校验

回填接口 `POST /api/local-ai/fill` SHALL 对每条提交项校验：`repo` 存在于仓库表、`kind` ∈ {translate, week, quarter, total, summary}、`period_label` 与当前期次一致（week = 当周标签、quarter = 当季标签、total/summary 固定 `all`、translate 忽略）、`text` 清洗后非空且不超过长度上限；`translate` 条目 SHALL 额外要求 `src` 必填、形如 8 位十六进制、且等于该仓库**当前** `description_en` 的 sha1 前 8 位（等价自动路径写译文前的 `description_en = ?` 守卫）。不合规条目 MUST 计入 `errors` 明细（含仓库与原因），MUST NOT 阻断同批其他条目的写入。

#### Scenario: 期次过期整条拒绝
- **GIVEN** 任务导出时周标签为 `2026-W37`，回填发生在新的一周
- **WHEN** 提交 `kind=week, period_label=2026-W37`
- **THEN** 该条计入 errors（原因指向期次已过期、需重新导出），不写库

#### Scenario: 仓库不在池
- **WHEN** 提交的 `repo` 在仓库表不存在
- **THEN** 该条计入 errors，其余条目照常处理

#### Scenario: 文本为空或超长
- **WHEN** 某条 `text` 清洗后为空字符串，或超过长度上限
- **THEN** 该条计入 errors，不写库

#### Scenario: 译文的原文指纹不符或缺失
- **GIVEN** 某 translate 任务导出时原文指纹为 `0a7d8ca5`
- **WHEN** 提交回填且 `src` 缺失、非法，或与当前 `description_en` 的指纹不一致
- **THEN** 该条计入 errors（原因指向原文已变更、需重新导出该条），不写库，其余条目照常处理

### Requirement: 回填文本最小清洗

回填接口 SHALL 在写库前对 `text` 做最小清洗：去除首尾空白、成对包裹的引号（中英文引号）与"推荐理由：/推荐语：/译文："类前缀。清洗 MUST NOT 改写正文其余内容。

#### Scenario: 本地工具带前缀
- **WHEN** 提交 `text = "推荐理由：这是一个数据管道工具。"`
- **THEN** 入库文本为 `"这是一个数据管道工具。"`

### Requirement: 回填写入语义

回填接口 SHALL 按以下语义写入：

- 默认只补缺失：目标行已存在时计入 `skipped`；请求体 `overwrite=true` 时才执行覆盖（`INSERT OR REPLACE`）；
- 所有回填写入的行 SHALL 标记 `source='manual'`；
- `week` / `quarter` 行写入 `generated_week` = 当前周标签、`readme_sha` 保持 NULL（与自动路径同口径）；
- `total` / `summary` 行写入回填时重新拉取的 README blob sha 作为指纹；拉取失败写 NULL；
- `translate` 写入 `repos.description_zh`，条件与自动路径护栏一致（`description_zh IS NULL AND description_en` 未变）；不满足条件且未开启 `overwrite` 时计入 `skipped`；- 单条写入即提交事务（与既有"单条 commit"口径一致），响应返回 `written` / `skipped` / `errors` 计数与明细。

#### Scenario: 默认不覆盖既有文本
- **GIVEN** 某仓库当前周 week 行已存在（`source='ai'`）
- **WHEN** 提交同键回填且未开启 overwrite
- **THEN** 该条计入 skipped，库内文本不变

#### Scenario: 显式覆盖
- **WHEN** 提交同键回填且 `overwrite=true`
- **THEN** 库内文本被替换，该行 `source` 变为 `manual`

#### Scenario: 重复回填幂等
- **WHEN** 同一批回填请求连续提交两次（未开启 overwrite）
- **THEN** 第二次全部计入 skipped，库内文本与 `source` 不变

#### Scenario: 简介已变更的译文回填
- **GIVEN** 导出后该仓库 `description_en` 已变化（导出时的 `src` 指纹与当前原文指纹不符）
- **WHEN** 提交 translate 回填
- **THEN** 该条计入 errors（原因指向原文已变更、需重新导出该条），不写库；重新导出后按新原文生成再回填

### Requirement: 人工行对自动路径的豁免

系统 SHALL 保证所有自动生成路径（每日 ensure、手动批量补缺 worker）MUST NOT 覆盖 `source='manual'` 的行：窗口日的 quarter/total/summary 重生判定在遇到人工行时直接跳过（不拉 README、不比对 sha、不调用 AI）；缺失行照常补缺并写入 `source='ai'`。周榜为每期次新行，人工行既不阻止新期次生成，也不被新期次写入影响。手动单仓"生成推荐语/重新生成"接口属用户显式操作，不受本豁免限制，其写入标记 `source='ai'`。

#### Scenario: 窗口日人工 total 行不被重生
- **GIVEN** 某仓库 total 行 `source='manual'`，README 在窗口日前已变更
- **WHEN** 每日 ensure 在窗口日跑批
- **THEN** 该行不被替换、指纹不被改写；同仓库若缺 summary 行则照常补缺

#### Scenario: 手动单仓重生不受限
- **GIVEN** 某仓库 week 行为 `source='manual'`
- **WHEN** 用户点击行内"重新生成"
- **THEN** 该行被新文本覆盖并标记 `source='ai'`

#### Scenario: 人工周榜行不影响新期次
- **GIVEN** 某仓库本周 week 行为 `source='manual'`
- **WHEN** 进入下一个 ISO 周并跑批
- **THEN** 系统照常为新周标签生成新行（`source='ai'`），上周人工行保留

### Requirement: 接口鉴权与体量上限

两个接口 SHALL 复用既有访问控制（Caddy Basic Auth 一层），MUST NOT 引入匿名入口或独立鉴权体系。导出接口 SHALL 对 `limit` 设上限；回填接口 SHALL 对请求体大小与单批条目数设上限，超限请求整批拒绝并给出明确错误。

#### Scenario: 超出 limit 上限
- **WHEN** 请求 `limit` 大于允许上限
- **THEN** 接口按上限截断（或拒绝），不返回无限量任务

#### Scenario: 请求体超限
- **WHEN** 回填请求体超过大小上限
- **THEN** 整批拒绝并返回明确错误，不写库
