# 提案：语言榜扩容（expand-language-boards）

## Why

"其它语言"池已膨胀到 4087 个仓库（占全池 38%），周榜 Top50 被十几个语言分食，单语言热度被稀释、肉眼难跟踪。本周快照实测（2026-10-10 vs 10-03）：C++ 776 池/41 个周增≥100、C 418/21、C# 309/15、Kotlin 211/31、Swift 159/21——五个语言体量与活跃度均足以独立成榜。2026-10-11 主 agent 裁定：语言榜 6 → 11，新增 C++/C/C#/Kotlin/Swift。

## What Changes

- **`app/classify.py` 的 `LANGUAGES` 扩容**：新增 5 个精确名→key 映射——`"C++"→cpp`、`"C"→c`、`"C#"→csharp`、`"Kotlin"→kotlin`、`"Swift"→swift`（key 全小写无符号，URL/参数安全）；插入位置在既有 6 语言之后，dict 保序即榜单展示顺序。
- **榜单集合用户可见口径变更**：三口径分类榜 17 张 → 22 张（语言 7 → 12：11 语言＋其它语言；主题 10 张不变）；边栏语言组 7 项 → 12 项；`?board=` 合法值新增 `language-cpp`/`language-c`/`language-csharp`/`language-kotlin`/`language-swift`，默认首榜仍为 `language-java`。
- **派生面自动跟随**（均从 `LANGUAGES` 派生、无第二数据源）：P6 关注页语言分组（C++/C/C#/Kotlin/Swift 仓从"其它语言"组移入各自语言组）、P8 智能搜索意图理解的 languages 合法值（新增 GitHub 精确名 "C++"/"C"/"C#"/"Kotlin"/"Swift"）。
- **不动的部分**：分类仍精确匹配、无语言仓仍归"其它语言"；Shell/HTML/Jupyter/PHP 不拆（裁定：HTML/Jupyter/无语言主要是教程内容仓，拆出来是内容榜而非语言榜——"教程/资源"主题榜留作未来候选，本 change 不做；PHP 活跃度撑不起榜）；主题榜、采集、打标、翻译/推荐链路一概不变。
- **历史数据无迁移**：语言分类是纯函数实时/预计算得出，board_cache 旧数据次日预计算自然刷新。

## Capabilities

### New Capabilities

- `language-boards`: 语言分类与语言榜集合的行为契约——`LANGUAGES` 精确名→榜单 key 映射与展示顺序、未列出语言/无语言归"其它语言"的口径、以及语言集合向榜单/边栏/关注分组/智能搜索的派生一致性。

### Modified Capabilities

（无——仓内既有 specs（tag-categories、new-project-zone、board-hide-tagged、smart-search、ai-refresh-cadence）均未覆盖语言分类需求；本次为新增 capability，不改既有 spec 条目。）

## Impact

- **代码**：`app/classify.py`（`LANGUAGES` 增 5 项＋注释/docstring 同步）；`app/web/routes.py` `LANG_COLORS` 补 C#/Kotlin/Swift 三色（2026-10-11 拍板，GitHub Linguist 官方色）；`app/report.py`、`app/schema.sql` 仅注释中"17 榜/语言 7"等数量表述刷新（榜单装配、白名单、边栏、P6 分组全部从 `LANGUAGES` 动态派生，零逻辑改动）；`README.md` 数量表述 17 → 22。
- **测试**：`tests/test_classify.py`（新增语言断言、`classify_language("C++")` 现归 other 的断言反转、顺序断言更新）；`tests/test_report.py`（Kotlin 仓落 other 的用例须换语言、17→22 结构断言）；`tests/test_board_cache.py`、`tests/test_web.py`（17/18 计数 → 22/23）。
- **数据**：无 schema 变更、无迁移；board_cache 次日自然刷新。
- **外部依赖**：无新增。
- **项目 SOP**：改用户可见口径（榜单集合扩容）→ OpenSpec change 先行（本件）；产品层冻结物验收通过后按 §1 口径回写（2026-10-11 拍板范围：《共识文档》§6、《交互流程说明》、《需求清单》、《功能闭环清单》、《交互流程/12、13》专章的旧数字全部回写；《示意图.html》为视觉冻结物不动，记已知漂移留版本复盘）；视觉为既有榜块/边栏组件复用、无新视觉元素，免示意图档；A 级预演（真实库 precompute_boards 验证 11 语言榜成形）＋真实页面截图交付本人走查。
