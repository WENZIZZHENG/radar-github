# 规格 delta：主题分类（topic-classification）

## Purpose

主题分类与主题榜成员归属的行为契约：封闭主题词表（config/topics.yaml）是主题榜成员的唯一判定依据，本规格钉死命中规则、多榜重复与"其他"榜口径、词表封板治理与变更生效链路，并锁定 2026-10-11 清理后的词表口径。

## ADDED Requirements

### Requirement: 主题分类唯一路径
主题分类 SHALL 只有一条判定路径：以封闭词表 `config/topics.yaml`（主题 key → label/words）对仓库 repositoryTopics 做词表匹配。系统 MUST NOT 引入第二条分类路径：不做权重/打分机制，不做模糊或前缀匹配，AI 候选词扫描结果 MUST NOT 绕过词表直接决定榜单归属。语言分类（LANGUAGES 精确匹配）不受本 capability 约束。

#### Scenario: 唯一路径
- **WHEN** 任意榜单（预计算或页面实时降级路径）需要判定某仓库的主题归属
- **THEN** 判定依据仅为当前生效的 topics.yaml 词表匹配结果，不存在其他归类来源

#### Scenario: AI 候选词不直接生效
- **WHEN** 每周候选词扫描产出高频未命中词与 AI 归类建议
- **THEN** 建议仅供本人过目，词表不变则任何仓库的主题归属不变

### Requirement: 主题命中规则
命中判定 SHALL 为：仓库 topics 值（防御性 lower/strip 归一后）等于某词条，或等于某词条＋尾"s"——词表只写单数即可覆盖复数 topics。规则 MUST 保持单向：词条为复数不命中单数 topic，且 MUST NOT 做通用去 s（防 css 被剥成 cs 之类误伤）。

#### Scenario: 精确命中
- **WHEN** 仓库 topics 含 `react`
- **THEN** 命中 frontend 主题

#### Scenario: 尾 s 归一命中
- **WHEN** 仓库 topics 含 `ai-agents`（词表词条为 `ai-agents` 精确命中；同理词条 `ai-agent` 存在时 topics 值 `ai-agents` 亦命中）
- **THEN** 命中 ai 主题

#### Scenario: 反向不命中
- **WHEN** 词表词条为复数形态而仓库 topics 为其单数形态
- **THEN** 不命中

#### Scenario: 防御性归一
- **WHEN** 仓库 topics 含大写或首尾空白变体（如 ` React `）
- **THEN** 归一后参与判定，命中不受影响

### Requirement: 主题榜成员归属口径
系统 SHALL 按命中结果归组：命中多主题的仓库在每个命中主题榜各出现一次（允许跨榜重复）；同一主题命中多个词条只进一次该榜；零命中仓库进"其他"榜（"其他"不是词表主题 key，由榜单层统一定义）。主题榜集合 = 词表主题 + 1 张"其他"榜，榜数随词表主题数变化，不写死。

#### Scenario: 多主题多榜重复
- **WHEN** 某仓库 topics 同时命中 frontend 与 backend 词条
- **THEN** 该仓库在 frontend 榜与 backend 榜各出现一次

#### Scenario: 零命中进其他榜
- **WHEN** 某仓库 topics 不命中任何词条
- **THEN** 该仓库只进"其他"主题榜

### Requirement: 词表封板治理
`config/topics.yaml` SHALL 为词表唯一权威版本（git 管理，本人封板才生效）。增删词条 MUST 本人确认，并同步《架构决策记录》附录词表块与 `tests/test_classify.py` 的词数锁定断言，三者一字/一数一致（锁断言漂移即 verify 红，为防漂移设计）。

#### Scenario: 增删词条同步
- **WHEN** 词表增删任何词条
- **THEN** topics.yaml、架构决策记录附录、词数锁定断言三处同批更新，统一验证入口跑绿

### Requirement: 词表变更生效链路
词表变更 SHALL 经下述链路生效：页面层词表为模块级缓存（运行期不变），MUST 重启进程加载新词表；board_cache 不含词表版本失效判定，词表变更后 MUST 手动重跑预计算刷新缓存，否则页面继续展示旧词表口径直至次日每日任务自动重算。

#### Scenario: 手动预计算立即生效
- **WHEN** 词表变更后重启服务并手动执行一次预计算
- **THEN** 主题榜成员按新词表口径落缓存，页面即展示新口径

#### Scenario: 不重跑则次日生效
- **WHEN** 词表变更后未手动预计算
- **THEN** 当日页面维持旧口径，次日 05:00 每日任务以新词表重算后生效

### Requirement: 2026-10-11 封板词表口径
本次清理后词表 SHALL 为 9 主题（顺序锁定：ai、frontend、backend、data、devops、devtools、security、game、automation），各主题词数锁定：ai 122、frontend 46、backend 54、data 36、devops 21、devtools 35、security 41、game 19、automation 18（总 392）。label 锁定：AI与智能、前端/UI、后端/云原生、数据库与数据工程、DevOps/监控、开发者工具、安全、游戏/图形、自动化/脚本。

#### Scenario: 词数锁定
- **WHEN** 加载 config/topics.yaml
- **THEN** 主题顺序与各主题词数等于上述锁定值，任一漂移即测试告警

#### Scenario: 清理后典型归属
- **WHEN** 以清理后词表判定：topics 含 `ssr` 的仓（如 nuxt）、含 `ci` 的仓（如 chinese-poetry）、含 `agent`/`agents` 的仓（如 dokploy）、含 `editor` 的仓（如 OpenCut）
- **THEN** 不再分别进入 security、devops、ai、devtools 主题榜（无其他命中时进"其他"榜）
