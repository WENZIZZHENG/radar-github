# 设计：主题榜扩容（expand-topic-boards）

## Context

现状（探仓确认）：主题分类由 `app/classify.py` 的 `classify_topics` 纯函数承担——封闭词表 `config/topics.yaml`（topic-wordlist-cleanup 清理后 9 主题、392 词位，工作区已落地）对 repositoryTopics 做精确匹配＋尾"s"归一（决策 3 v2）。判定逐主题独立（每主题 `any(word in normalized or word+"s" in normalized)`），天然支持一词多主题。消费方全部从词表动态派生、无第二份主题清单：榜单装配 `app/report.py`（主题桶遍历词表）、边栏白名单 `app/web/routes.py:645-652`（`_board_key_whitelist` 动态生成）、边栏主题组同源于此。生效链路两处缓存事实（与 topic-wordlist-cleanup 相同）：`routes.py:128-136` `_topic_table()` 模块级缓存（词表改动需重启进程）；board_cache 失效判定不含词表版本（变更后须手动预计算，否则次日 05:00 每日任务自动重算；`routes.py:763-766` 已有窗口期降级保护——缓存 payload 查无新榜 key 自动降级实时算，不 500）。主题榜无边栏点色（`routes.py:601`：LANG_COLORS 只服务语言榜，主题榜统一灰点），新主题无需配色。动机与实测数字见 proposal.md（Why）。

约束：S 档单人项目；fail-loud 既定姿态；裁定清单（2026-10-11 本人拍板）钉死主题集合/key/label/词表/顺序/双榜破例/文档范围，不许变更；词表为封板物（决策 12），proposal.md 逐词 diff 表经本人过目封板后才动 topics.yaml。

## 路径唯一性自查（裁定 9）

- 主题分类有几条路径？**唯一一条**：`classify_topics` × `config/topics.yaml` 词表匹配。预计算路径（jobs.py 任务体内现场 `load_topics`）与页面实时降级路径（`_topic_table()` 模块缓存，同一文件）只是同一判定的两种触发时机，不构成第二条路径。
- 本次是否新增第二条路径？**否**——只追加词表数据，不改匹配逻辑、不引权重机制、零代码逻辑改动；无第二份词表。
- AI 候选词扫描（T-032，决策 12）只产建议清单落库展示，不写词表、不影响榜单归属，不构成第二条分类路径（本变更不动它）。
- 本 change 不涉及入口/注册/登录/权限/删除/数据归属——无相关路径可言。

## Goals / Non-Goals

**Goals:**
- `config/topics.yaml` 追加 6 主题 70 词位（392 → 462 词位 / 457 全局唯一词），顺序 learning→mobile→embedded→media→selfhosted→blockchain 插 automation 尾部。
- 封板物三处一字/一数同步：topics.yaml（含头注释扩展封板记录）、《架构决策记录》附录（yaml 块＋收词沿革）、test_classify.py 词数/顺序/label 锁断言。
- 词表变更经"重启进程＋手动预计算"链路对页面生效，本地真实库 A 级预演验证 6 张新榜成形。

**Non-Goals:**
- 不改 `classify_topics` 匹配逻辑（无权重、无模糊匹配、无代码逻辑变更）。
- 不动现有 9 主题的顺序/label/词表；不动语言榜 12 张与 `LANGUAGES`。
- 不做"其他"榜展示层治理（2,397 仓无任何 topics，词表救不了，维持 topic-wordlist-cleanup 拍板：接受膨胀，记已知后续候选）。
- 不做主题榜边栏点色（主题榜统一灰点为既有口径）；不做词表运行时配置化。
- 不动《示意图.html》《原型-T021.html》（视觉冻结物，漂移并入已知登记，见决策 8）。

## Decisions

### 决策 1：只改词表数据，匹配逻辑零改动（裁定 1/2 范围内）
扩容属词表数据问题而非规则问题；`classify_topics` 逐主题独立判定，新增主题与一词多主题均无需任何代码改动即天然支持。备选：引入权重/主主题机制或命中量排序——否决，裁定明确不做，S 档不需要。

### 决策 2：6 新主题追加尾部、顺序锁定（裁定 2 钉死，此处留痕）
排列 learning→mobile→embedded→media→selfhosted→blockchain，插现有 9 主题之后（automation 尾部）；YAML dict 保序即榜单展示顺序，主题榜组内顺序随之锁定。备选：按实测命中量穿插重排全部 15 主题——否决，"现有 9 主题顺序不动"是裁定，追加尾部零漂移、diff 最小且过目成本最低。

### 决策 3：双榜破例——mobile 与 frontend 共有 5 词（裁定 3，一词多主题首次，理由显式记录）
`flutter`/`react-native`/`swiftui`/`jetpack-compose`/`kotlin-multiplatform` 同时保留在 frontend 词表并收录进 mobile 词表，标这些词的仓同时进两榜。理由：①既有机制天然支持——`classify_topics` 逐主题独立判定，一词多主题零代码改动；②语义双属——这些词既是前端/UI 框架技术（跨端 UI 体系），也是移动平台开发主力词，砍掉任一归属都丢真实信号；③frontend 不搬家——搬家会抽掉 frontend 榜既有真实命中成员（flutter 仓在"前端/UI"主题的归属同样成立），且违反"现有 9 主题词表不动"裁定。代价与兜底：词表治理"一词一主题"惯例首次打破（全局唯一词 457 < 词位 462），此后收词评审须显式过目共有词；词数锁断言锁定新分布防静默扩散（决策 12 本人封板流程不变）。

### 决策 4：不收词清单及理由（裁定 4，撞车纪律留痕）
- `streaming`：topic-wordlist-cleanup 刚删的泛词（流媒体应用撞数据流后端语义），不复活。
- `crypto`：抽样 7 仓含 MPC 密码学/I2P/CA 工具，撞安全语义。
- `kernel`：19 仓一半是 SerenityOS/reactos 等 OS 开发，嵌入式纯度不足。
- `list`/`documentation`：词义散，文档工具 ≠ 学习资源。
- `blog`/`notes`/`study`/`roadmap`：泛或量级 ≤7。
- `android-app`/`android-application`/`ios-app`/`mobile-app`/`apk`/`termux`/`expo`：长尾量级 ≤7，宁缺毋滥。
- `arm`/`arm64`/`bluetooth`：架构支持泛词（各类仓附带声明，非嵌入式语义）。
- `smart-home`/`nas`/`immich`/`self-hosting`：量级 ≤3。
- `defi`/`nft`：零命中仓中量级 0。
- `sound`/`radio`/`synthesizer`/`audio-processing`：泛或量级小。
- `platformio`/`freertos`：量级 ≤1。

### 决策 5：词表编辑保持既有形态（裁定 6）
直接编辑 `config/topics.yaml` 行内流式数组追加 6 主题块；数组内词序按"主力词在前、语义相近相邻"书写（与现有词表风格一致）；头注释命中规则句不动，追加"2026-10-11 扩展封板（+6 主题 70 词位，392→462 词位/457 唯一词）"。《架构决策记录》附录 yaml 块一字同步（T-004 机器 diff 验证口径），收词沿革补一条："2026-10-11 词表扩展 +6 主题 70 词位（392→462 词位/457 唯一词）——'其他'榜 45% 膨胀治理，本人拍板逐词清单，OpenSpec change `expand-topic-boards`"。三处（yaml/附录/锁断言）同一批改动提交，缺一 verify 红。
词形修正留痕（2026-10-11 主 agent 抽查起草产物时裁定修正，词位/唯一词总数不变）：① learning 删 `algorithms`——尾"s"归一规则下词条 `algorithm` 已命中 topics `algorithms`，复数词条冗余；② blockchain 增 `cryptocurrencies`——y→ies 复数变体不受尾"s"归一覆盖（词条 `cryptocurrency` 只命中 `cryptocurrency`/`cryptocurrencys`），零命中仓实测 5 仓会漏。

### 决策 6：测试适配口径（裁定 7）
- `tests/test_classify.py`：词数锁断言（:165-178）更新为 15 主题新分布；主题顺序锁（:135-147）更新为 15 项新序列；label 锁（:150-162）增 6 个新 label；新增每主题至少一条精确命中用例（leetcode→learning、android→mobile、esp32→embedded、jellyfin→media、home-assistant→selfhosted、solidity→blockchain）＋一条单复数归一用例（cheatsheets→learning、androids→mobile、fpgas→embedded、podcasts→media、nextclouds→selfhosted、wallets→blockchain）；新增双榜断言 `classify_topics(["flutter"], table) == ["frontend", "mobile"]`（词表序 frontend 在前）。
- `tests/test_report.py:221-227`：docstring 与 `len(boards) == 22` → 28（`boards[12:]` 段断言本就动态跟随 `topic_table` fixture，无需改）。
- `tests/test_board_cache.py:143`（`len(real) == 22`→28）、`:279-282`（摘要断言 `"boards": 22`→28 三处）。
- `tests/test_web.py`：计数常量 22/23 → 28/29（:4-5 docstring、:165-184、:190-202、:831-853、:943-962 含 :953"其余 21 榜"→27、:1049-1107，以实际断言行号为准）——参照 expand-language-boards 同口径。

### 决策 7：生效链路钉死顺序——先重启、再手动预计算
与 topic-wordlist-cleanup 决策 5 同链路：① 部署新词表并重启进程（重建 `_topic_table_cache`，页面边栏白名单与实时降级路径才用新词表；生产部署流程本就 `systemctl restart radar`）；② 手动跑一轮 `precompute_boards` 刷新 board_cache（本地命令见 topic-wordlist-cleanup design.md 决策 5；生产命令形态见 docs/dev-commands.md）。顺序不可反；不手动跑则次日 05:00 每日任务自动以新词表重算。窗口期保护既有：`routes.py:763-766` 缓存 payload 查无新榜 key 自动降级实时算。

### 决策 8：产品层文档回写范围与视觉冻结物漂移登记（裁定 5）
验收通过后按 §1 口径全量回写：《需求清单》（:38 成功标准"22 个榜"→28）、《功能闭环清单》（:8 功能 1"主题榜 10 块"→16 块）、《共识文档》（:49"主题榜单（10 块）"→16 块）、《交互流程说明》（:10、:19、:54、:58、:138 的 22 榜/主题榜 10 块/10 项表述）、《交互流程/12-左侧边栏导航与P6标签筛选.md》（:6、:7 主题榜 10 项→16 项、22 榜→28）、《交互流程/13-单榜整页与移动端行布局.md》（:3、:7 全量 22 榜→28）、README（:10-11 数量表述）。《示意图.html》《原型-T021.html》不动：其"主题 10 项"等表述与实现的差异并入 expand-language-boards 已登记的已知漂移，留版本复盘处理。

### 决策 9：delta 形态——MODIFIED 锁定要求 + ADDED 双榜要求，归档排在 topic-wordlist-cleanup 之后（起草实测定形）
本 change 落地后词表锁定口径即过时（topic-wordlist-cleanup 引入的"2026-10-11 封板词表口径"锁 9 主题 392 词），语义正确形态是 MODIFIED 更新该要求为 15 主题 462 词位 + ADDED 一条"一词多主题双榜归属"。`openspec validate --strict` 实测：✓ 通过，附 INFO——主规格库尚无 topic-classification（topic-wordlist-cleanup 未归档），MODIFIED delta 的 archive 会拒绝。即**本 change 的 archive 必须排在 topic-wordlist-cleanup 归档之后**（代码层面词数锁本就以其 392 分布为基线，顺序依赖一致）；若违反顺序，archive 直接报错拒绝（fail-loud，不会静默产出矛盾规格）。备选：全用 ADDED 另立"扩展封板词表口径"要求——否决，两 change 归档顺序正常时主规格会同时残留 9 主题/392 与 15 主题/462 两条互相矛盾的锁定要求，属静默语义债。

### 决策 10：增带 language-boards MODIFIED delta，归档顺序钉死三链（2026-10-11 主 agent 拍板，收起草存疑点 2）
本 change 附 `specs/language-boards/spec.md` MODIFIED delta：把 expand-language-boards 引入的"语言榜集合与展示顺序"要求中"共 22 张/主题组 10 张"更新为"共 28 张/主题组 16 张"（Scenario 同步）。理由：主题榜 10→16 是本 change 的直接后果，language-boards 中引用主题榜数量的表述应随本 change 更新。归档顺序钉死：**topic-wordlist-cleanup → expand-language-boards → expand-topic-boards**——本 change 的两个 MODIFIED delta 分别依赖前两者沉淀的 topic-classification / language-boards 主规格，乱序 archive 会被拒（fail-loud）。

## Risks / Trade-offs

- ["其他"榜瘦身幅度有限：2,397 仓无任何 topics，词表救不了] → 预期内（topic-wordlist-cleanup 已拍板接受膨胀）；本次实际上限是 2,439 有 topics 未命中仓，A 级预演记录瘦身幅度与 6 新榜行数，交付本人过目。
- [双榜重复仓增多（flutter 等 5 词同时进 frontend 与 mobile）] → 裁定 3 拍板留双榜；多榜重复本即主题榜成员归属口径，非 bug。
- [一词多主题首例削弱"全局唯一词"防漂移直觉] → 词数锁断言锁定 462 词位/457 唯一词新分布；后续收词仍走决策 12 本人封板，共有词须显式过目（决策 3）。
- [新主题词误伤既有主题命中分布] → 裁定 1 词表已经主 agent 零命中仓逐词实测命中量＋抽样纯度验证；6 主题词表与现有 9 主题词表除裁定 3 的 5 个共有词外零交集。
- [忘重启导致页面层词表缓存仍旧] → 决策 7 钉死"先重启再预计算"；生产部署本就重启。
- [封板物三处漂移] → 词数锁断言＋附录一字同步要求即防漂移设计，verify 红即拦截。
- [边栏主题组 10→16 项变更长，窄屏/移动端边栏更长] → 边栏既有 sticky＋可收起形态不变；主题榜无点色，视觉零新增；28 榜 SSR 体积增量 ≈ 6 榜 × 50 行，单榜整页（§13.1）已把默认页收窄为单榜，可接受。
- [archive 顺序依赖（决策 9）：先于 topic-wordlist-cleanup 归档会被拒] → Migration Plan 步骤 0 钉死前置；archive 拒绝即 fail-loud 拦截，无静默风险。

## Migration Plan

0. **前置**：topic-wordlist-cleanup 已归档（其词表基线 392 分布已在工作区落地；本 change delta 的 MODIFIED 操作依赖其沉淀的 topic-classification 主规格，见决策 9）。
1. 本人封板 proposal.md 逐词 diff 表（6 主题 70 词位）；
2. 同批改 topics.yaml＋架构决策记录附录＋test_classify.py（缺一 verify 红）；
3. `tools/verify.ps1` 跑绿；
4. A 级预演：本地真实库 data/radar.db 手动预计算，记录 6 个新主题榜行数、"其他"榜瘦身幅度、典型仓抽查（system-design-primer/CyC2018-CS-Notes 应进学习资源榜、termux 系进移动榜、zephyr 进嵌入式榜、jellyfin 进音视频榜、home-assistant 生态进自托管榜、viem/optimism 进区块链榜）；
5. 部署生产 → 重启 → 手动预计算 → 冒烟（新主题榜成员抽查）。
回滚：`git checkout` 旧词表三件套 → 重启 → 重跑预计算（纯配置回滚，无数据迁移，board_cache 可再生成）。

## Open Questions

（词表/顺序/双榜/文档范围均已于 2026-10-11 本人拍板收口，无遗留待定项。以下一条为起草中发现的跨 change 规格一致性问题，已拍板收口。）

- ~~**expand-language-boards delta 的榜数表述将过时**~~ → **已裁定（2026-10-11 主 agent 拍板，决策 10）**：本 change 增带 `language-boards` MODIFIED delta（`specs/language-boards/spec.md`），把"语言榜集合与展示顺序"要求的"共 22 张/主题组 10 张"更新为"共 28 张/主题组 16 张"。理由：主题榜 10→16 是本 change 的直接后果，language-boards 中引用主题榜数量的表述本就该随本 change 更新；备选①（改 expand-language-boards 的 delta 文本）否决——其"22 张"是该 change 落地时的真实变化，且归档顺序它在先；备选③（归档后专项修）否决——主规格会残留自相矛盾窗口。归档顺序因此钉死为：topic-wordlist-cleanup → expand-language-boards → expand-topic-boards（本 change 的 MODIFIED 依赖前两者的主规格沉淀）。
