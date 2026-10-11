# 任务清单：语言榜扩容（expand-language-boards）

> 流程口径（项目 SOP）：本变更无新交互/新视觉元素（既有榜块/边栏组件复用），免示意图档；文字层改动为"数量表述刷新"而非新流程，随代码同批推进、验收通过后按 §1 口径回写冻结物；A 级预演（核心链路：每日采集入库 → 榜单计算 → 榜单/关注页展示）。

## 1. 分类器与代码触点

- [x] 1.1 `app/classify.py:21-28` `LANGUAGES` 增 5 项（保序插尾部）：`"C++": "cpp"`、`"C": "c"`、`"C#": "csharp"`、`"Kotlin": "kotlin"`、`"Swift": "swift"`；同步模块 docstring（:3）与 dict 注释（:19-20）——"6 个指定语言"→"11 个指定语言"，顺序说明更新为"共识文档 §6 列举顺序＋新增五语言（C++/C/C#/Kotlin/Swift）"，`classify_language` docstring（:59）同步
- [x] 1.2 注释/docstring 数量表述刷新（零逻辑改动）：`app/report.py:343,346`（"语言榜 7 张"/"共 17 张榜"→12/22）；`app/web/routes.py:376`（P6"按语言分 7 组"）、`:639`、`:643`、`:729`（"17 榜"类表述）；`app/schema.sql:94`（board_cache 注释"全量 17 榜"）；`app/web/templates/boards.html:60`（"全量 17 榜"注释）
- [x] 1.3 `README.md:10` "各 17 张分类榜（6 语言＋其它语言、9 主题＋其他）" → "各 22 张分类榜（11 语言＋其它语言、9 主题＋其他）"；`README.md:11` "三口径 17 榜落 board_cache" → 22
- [x] 1.4 确认零逻辑改动面：`app/report.py:378,465-466`（榜单装配）、`app/web/routes.py:123,642-649,661`（反向映射/白名单/默认首榜）、`app/search.py:147-148`（搜索语言归一）全部从 `LANGUAGES` 派生——逐一核对无硬编码语言清单残留（grep `LANGUAGES` 全仓复查）
- [x] 1.5 `app/web/routes.py:106-119` `LANG_COLORS` 补三色（2026-10-11 拍板必做，GitHub Linguist 官方色口径）：`"C#": "#178600"`、`"Kotlin": "#A97BFF"`、`"Swift": "#F05138"`
- [x] 1.6 `app/ai.py` P8 意图理解 prompt 语言枚举（首搜/追问两套 system）从写死 6 语言改为 `"/".join(LANGUAGES)` 动态拼接（design.md Context 裁定的规格内触点，评审 F3-1 补录本行）

## 2. 测试适配

- [x] 2.1 `tests/test_classify.py`：新增 5 语言精确匹配断言；`:36` `classify_language("C++") == OTHER_LANGUAGE_KEY` 反转为 `== "cpp"`（行为变更点）；`:48-55` 顺序断言更新为 11 项新序列（既有 6 项序不变，尾部追加 cpp/c/csharp/kotlin/swift）
- [x] 2.2 `tests/test_report.py:180-191`：`a/kotlin-app` 改用语言 "PHP"（保持"未列出语言落 other"用例语义）；`:221-227` 结构断言 17→22、`boards[:7]`→`[:12]`、"其它语言"落位 index 6→11
- [x] 2.3 `tests/test_board_cache.py:143`（`len(real) == 17`→22）；`tests/test_web.py` 计数常量 17/18 → 22/23（:5、:166-181、:841、:849、:943、:1049 附近，以实际断言行号为准）
- [x] 2.4 `tests/test_search.py` 补一条合法归一用例：languages 含 "c++"/"C#" → 归一为 "C++"/"C#"（锁定 P8 新合法值行为；既有 "Cobol"/"cpp" 非法丢弃用例不动）
- [x] 2.5 `tests/test_ai.py` 新增 `test_client_intent_prompt_languages_follow_classifier`：断言 prompt 中 `"取值只能来自 " + "/".join(LANGUAGES) + "，"` 逐字出现（词表变 prompt 必变的回归锁；MockTransport 仅替换 DeepSeek HTTP 边界，prompt 构造走真实代码路径；评审 F3-1 补录本行）

## 3. 验证

- [x] 3.1 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1`（ruff + pytest）一次跑绿
- [x] 3.2 真实库（`data/radar.db` 副本或只读连接）手动跑一次 `precompute_boards`：确认三口径各 22 榜成形、语言组顺序正确、C++ 榜有数十行（本周快照 41 个周增≥100 仓为参照量级）、Kotlin/Swift 榜非空、"其它语言"榜行数显著下降

## 4. 评审与交付

- [x] 4.1 独立评审子 agent，中/高 findings 清零（最多三轮）（agent-735：0 高/0 中/4 低，F3-1 补录 tasks 1.6/2.5 两行、F3-2 补录原型-T021.html 漂移登记，均已当场落地）
- [ ] 4.2 A 级预演＋真实页面截图（边栏语言组 12 项、`?board=language-cpp` 单榜直达、P6 关注页新语言分组）＋复验步骤卡 → 本人走查
- [x] 4.3 产品层文档回写（验收通过后按 §1 口径，2026-10-11 拍板全量范围）：《共识文档》§6（:25 "指定 6 语言"列举→11 语言、:49 "语言榜单（7 块）"→12 块）、《交互流程说明》（:10、:19、:54、:58、:138 等"语言榜 7 块/7 项"与 17 榜表述）、《需求清单》（:16 S1、:38 成功标准"17 个榜"→22）、《功能闭环清单》（:8 功能 1"语言榜 7 块"→12 块）、《交互流程/12-左侧边栏导航与P6标签筛选.md》（:6、:7"语言榜 7 项"→12 项、17 榜→22）、《交互流程/13-单榜整页与移动端行布局.md》（:3、:7 全量 17 榜→22）
- [x] 4.4 《示意图.html》本次不动（视觉冻结物）：其"17 榜/语言 7 项"表述与实现的差异记为已知漂移，留版本复盘处理（design.md Open Questions 留痕）；《原型-T021.html》同口径并入已知漂移（评审 F3-2）
- [ ] 4.5 通过后当场归档（openspec archive；属产品层口径的同步回写 docs/sop 对应冻结物），文档按 §5 节奏提交
