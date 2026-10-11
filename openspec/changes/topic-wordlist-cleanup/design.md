# 设计：主题词表清理（topic-wordlist-cleanup）

## Context

现状（探仓确认）：主题分类由 `app/classify.py` 的 `classify_topics` 纯函数承担——封闭词表 `config/topics.yaml`（9 主题、410 词条）对 repositoryTopics 做精确匹配＋尾"s"归一（决策 3 v2）。榜单层 `app/report.py` 的 `compute_boards` 按命中结果归桶，`precompute_boards` 每日采集后跑三口径落 `board_cache`；页面直读缓存，缺失/过期才降级实时算。两处缓存事实决定生效链路：`app/web/routes.py` 的 `_topic_table()` 是模块级缓存（注释钉死"词表是封板物、运行期不变，改动需重启进程生效"）；`board_cache` 失效判定只看期次 label 与 `MAX(captured_at)`，不含词表版本。词表治理契约（决策 12）：topics.yaml 是唯一权威版本、本人封板才生效，附录与 `tests/test_classify.py` 词数锁定断言（当前锁 410 分布）必须同步，漂移即 verify 红。动机与逐词清单见 proposal.md。

约束：S 档单人项目；fail-loud 既定姿态；裁定清单（proposal 逐词 diff 表）不许变更。

## 路径唯一性自查

- 主题分类有几条路径？**唯一一条**：`classify_topics` × `config/topics.yaml` 词表匹配。预计算路径（jobs.py 每次任务现场 `load_topics`）与页面实时降级路径（routes.py `_topic_table()` 模块缓存，同一文件）只是同一判定的两种触发时机，不构成第二条路径。
- 本次是否新增第二条路径？**否**——只改词表数据，不改匹配逻辑、不引权重机制、零代码改动。
- AI 候选词扫描（T-032，决策 12）只产建议清单落库展示，不写词表、不影响榜单归属，不构成第二条分类路径（本变更不动它）。

## Goals / Non-Goals

**Goals:**
- topics.yaml 删 19 词、增 1 词（410 → 392），逐词清单与理由以 proposal.md diff 表为准。
- 封板物三处一字/一数同步：topics.yaml、《架构决策记录》附录、test_classify.py 词数锁定断言。
- 词表变更经"重启进程＋手动预计算"链路对页面生效，本地真实库验证榜单成形。

**Non-Goals:**
- 不改 `classify_topics` 匹配逻辑（无权重、无模糊匹配、无代码变更）。
- 不动 game、automation 词表；不动各主题 label 与主题顺序。
- 不做"其他"榜膨胀的展示层治理（2026-10-11 本人拍板：接受膨胀，记为已知后续候选）。
- 不动 AI 候选词扫描链路（T-032）与任何页面/交互。

## Decisions

### 决策 1：只改词表数据，匹配逻辑零改动（本人裁定）
错分根因是词义撞车与词条过宽，属数据问题而非规则问题；命中规则（精确＋尾 s 归一）本身无缺陷。备选：引入权重/主主题机制——否决，裁定明确不做，且 S 档不需要。

### 决策 2：词表编辑保持既有形态
直接编辑 `config/topics.yaml` 行内流式数组，主题顺序、label、缩进风格不变；头注释命中规则句不动，补一句封板记录（2026-10-11 清理封板，410→392）。devops 增补的 `continuous-integration` 排在 `continuous-delivery` 旁（语义成对，便于过目）。

### 决策 3：测试同步——`docker` 用例换 `kubernetes`，词数锁断言更新
test_classify.py 有 3 个用例依赖将被删除的 `docker`（backend）：`test_classify_topics_multi_theme_hits_duplicate_boards`、`test_classify_topics_result_follows_table_order_not_input_order`、`test_classify_topics_defensive_lower_strip`。统一改用保留词 `kubernetes`（backend 云原生骨干词，无撞车争议、无复数变体干扰），断言结构与锁定行为（多榜重复/结果顺序按词表序/防御性归一）不变。词数锁定断言更新为：ai 122、frontend 46、backend 54、data 36、devops 21、devtools 35、security 41、game 19、automation 18（总 392）。其余用例不依赖被删词条（`ai-agent`/`ai-agents`/`react` 均保留；无用例依赖 `editor`），不动。

### 决策 4：封板物三处同批更新（决策 12 既有口径）
《架构决策记录》附录 yaml 块与 topics.yaml 一字一致（T-004 机器 diff 验证口径），收词沿革补一条："2026-10-11 词表清理 −19/+1（410→392）——上榜仓系统性错分治理（词义撞车＋词条过宽），本人拍板逐词清单，OpenSpec change `topic-wordlist-cleanup`"。三处（yaml/附录/锁断言）必须同一批改动提交，缺一 verify 红。

### 决策 5：生效链路钉死顺序——先重启、再手动预计算
① 部署新词表并重启进程（重建 `_topic_table_cache`，页面边栏白名单与实时降级路径才用新词表；生产部署流程本就 `systemctl restart radar`）；② 手动跑一轮 `precompute_boards` 刷新 board_cache（本地：`python -c "from app.db import get_conn; from app.classify import load_topics; from app.config import BASE_DIR; from app.report import precompute_boards; c=get_conn(); print(precompute_boards(c, load_topics(BASE_DIR/'config'/'topics.yaml'))); c.close()"`；生产命令形态见 docs/dev-commands.md）。顺序不可反：先预计算后重启会出现"缓存榜单（新词表）与实时降级口径（旧词表）"不一致。不手动跑则次日 05:00 每日任务自动以新词表重算（jobs.py 任务体内现场 `load_topics`，无缓存）。

### 决策 6：devtools 删 `editor`、`compiler` 保留（2026-10-11 本人补充裁定）
`editor` 删除：OpenCut（视频剪辑）、AFFiNE（文档编辑器）、pascalorg（3D 编辑器）类"编辑器"泛词噪声大于收益；兜底无损——microsoft/vscode 有 `electron` 词仍留前端/UI 榜，不会从雷达消失。`compiler` 保留：rust-lang/rust 等真编译器需要它；nextjs/svelte 经此词进开发者工具榜属容忍噪声。测试面：test_classify.py 无用例依赖 `editor`（已 grep 核实），本裁定只影响词数锁断言（devtools 36→35）。

## Risks / Trade-offs

- [被删词条唯一命中的仓掉出主题榜，"其他"榜成员增多] → 预期内结果（错分仓本不该在主题榜）；A 级预演时记录各主题榜行数变化与"其他"榜增量，交付本人过目。
- [忘重启导致页面层词表缓存仍旧] → 决策 5 钉死"先重启再预计算"顺序；生产部署本就重启。
- [换词后测试断言语义弱化] → `kubernetes` 与 `docker` 同为 backend 精确词，三个用例锁的行为（多榜重复/顺序/归一）不变。
- [尾 s 归一残险变化] → 删 `agent`/`agents` 两词条后 topics 值 `agents` 不再命中 ai（裁定意图）；决策 3 v2 既有残险（`ai`→`ais`）不受本变更影响。
- [封板物三处漂移] → 词数锁断言＋附录一字同步要求即防漂移设计，verify 红即拦截。

## Migration Plan

1. 本人封板 proposal.md 逐词 diff 表；
2. 同批改 topics.yaml＋架构决策记录附录＋test_classify.py；
3. `tools/verify.ps1` 跑绿；
4. A 级预演：本地真实库 data/radar.db 手动预计算，核对榜单成形与典型仓归属；
5. 部署生产 → 重启 → 手动预计算 → 冒烟（主题榜成员抽查）。
回滚：`git checkout` 旧词表三件套 → 重启 → 重跑预计算（纯配置回滚，无数据迁移，board_cache 可再生成）。

## Open Questions

（两条均已于 2026-10-11 本人拍板收口，无遗留待定项。）

- **`editor`（devtools）保留确认** → **已裁定（2026-10-11 本人拍板）：删 `editor`**，理由与兜底见决策 6；`compiler` 保留。
- **"其他"榜膨胀观察** → **已裁定（2026-10-11 本人拍板）：接受膨胀**，本变更不做展示层治理，记为已知后续候选（本设计留痕即记录，后续若要治理从词表治理/展示策略另行立项）。
