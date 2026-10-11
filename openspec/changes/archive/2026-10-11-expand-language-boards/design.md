# 设计：语言榜扩容（expand-language-boards）

## Context

现状：语言分类集中在 `app/classify.py` 的 `LANGUAGES` dict（6 个 GitHub 精确名→小写 key，`classify_language()` 精确匹配，未列出/None 归 `OTHER_LANGUAGE_KEY="other"`）。全链路消费方**全部从 `LANGUAGES` 动态派生、无第二份语言清单**（探仓确认）：

- 榜单装配 `app/report.py:378,465-466`（`lang_buckets` 初始化与 `boards` 列表均遍历 `LANGUAGES`）；
- 边栏白名单 `app/web/routes.py:642-649`（`_board_key_whitelist` 动态生成）；默认首榜 `routes.py:661` 取 `LANGUAGES` 首项（java，不变）；
- P6 关注页分组 `routes.py:375-383,426`（`classify_language` 归组，组头名经 `_LANG_KEY_TO_NAME` 反查，`routes.py:123` 反向映射自动跟随）；
- P8 智能搜索语言归一 `app/search.py:147-148`（大小写不敏感匹配 `LANGUAGES` 键集）；
- P8 意图理解 prompt 语言枚举 `app/ai.py:447,462`（首搜/追问两套 system）：**原为写死 6 语言枚举（探仓漏项），2026-10-11 主 agent 裁定属本 change 规格内触点（spec R4 语言集合派生一致性），已改为从 `LANGUAGES` 动态拼接**（枚举值给 LLM 的是 GitHub 精确名＝dict 键集，非榜单 key）；
- 语言点色 `LANG_COLORS`（`routes.py:106-119`）已有 C++/C 两色。

约束：S 档单人项目；fail-loud 既定姿态；语言分类是纯函数、无存储语义（board_cache 次日预计算自然刷新）；主 agent 裁定清单钉死新增语言集合、key 形态与插入顺序（不许变更）。

## Goals / Non-Goals

**Goals:**
- `LANGUAGES` 扩为 11 项，新增项插既有 6 语言之后，顺序 C++/C/C#/Kotlin/Swift；key 全小写无符号。
- 三口径分类榜 17 → 22 张、边栏语言组 7 → 12 项，`?board=` 合法值增 5 个，默认首榜不变。
- 派生面（P6 分组、P8 搜索 languages、边栏/白名单）零逻辑改动自动跟随；仅刷新各层"17 榜/语言 7"注释与文档数量表述。

**Non-Goals:**
- 不拆 Shell/HTML/Jupyter/PHP/无语言（裁定 2："教程/资源"内容榜留作未来候选）。
- 不改主题榜、采集、打标、翻译/推荐任何链路；不改 schema、不做数据迁移。
- 不做语言榜的自定义开关/排序配置（集合即 `LANGUAGES` 常量，与主题词表不同，不做运行时配置化）。

## Decisions

### 决策 1：只改 `LANGUAGES` dict 一处，派生面零改动
探仓确认语言集合单一事实源就是 `LANGUAGES`，榜单/边栏/P6/P8 全部动态派生。本变更的代码改动 = `classify.py` 增 5 个映射项＋同步模块 docstring 与注释（"6 个指定语言"→"11 个指定语言"、顺序说明同步为"共识文档 §6 列举顺序＋新增五语言"）；其余代码文件只刷新"17 榜/语言 7"类注释表述（report.py docstring、routes.py `_board_key_whitelist`/`_follow_groups`/§13.1 注释、schema.sql board_cache 注释、README）。
备选：给 5 个新语言单独做一个追加列表再合并——否决，发明第二数据源违反单一事实源，dict 直接保序书写即展示顺序。

### 决策 2：新增 key 形态（裁定 1 钉死，此处留痕理由）
`C++`→`cpp`、`C#`→`csharp`：`+`/`#` 在 URL 查询参数中需转义（`+` 更是空格语义），key 必须全小写无符号；`cpp`/`csharp` 是 GitHub/业界惯用 slug 形态。`C`→`c` 单字母无歧义。边栏徽标 URL（`?board=language-cpp`）与 `_board_key_whitelist` 白名单形态（`kind-key`）天然兼容。

### 决策 3：默认首榜维持 language-java 不动
`_resolve_board` 缺省/非法 board 值降级到 `LANGUAGES` 首项。新增语言插尾部后首项仍是 java，既有书签/默认落地页行为零变化。备选：按池内体量重排语言顺序——否决，裁定 3 钉死插入位置，且既有用户肌肉记忆优先。

### 决策 4：历史数据与缓存不迁移
语言分类实时计算（无物化归类列），board_cache 存的是 Board JSON——旧缓存 payload 里 C++/Kotlin 仓仍在"其它语言"榜，次日每日 job 的 `precompute_boards` 落新 payload 自然刷新。最坏窗口 = 一天内页面仍显示旧分组，S 档可接受；也可部署后手动跑一次 precompute 立即生效（验收步骤含此动作）。
备选：部署即清 board_cache 强制重算——不必要，手动 precompute 更可控且验收本就要跑。

### 决策 5：测试适配口径
- `test_classify.py`：新增 5 语言精确匹配断言；**既有 `classify_language("C++") == OTHER_LANGUAGE_KEY` 断言反转为 `== "cpp"`**（行为变更点，测试须改不是放松标准）；HTML 仍归 other 的断言保留；顺序断言更新为 11 项新序列。
- `test_report.py`：`test_other_language_board` 现用 `language="Kotlin"` 造"落 other"用例——Kotlin 成榜后该断言失效，改用未列出语言（如 "PHP"）保持用例语义；17→22 结构断言（`boards[:7]`→`[:12]`、other 落位 index 6→11）。
- `test_board_cache.py` / `test_web.py`：17/18 计数常量 → 22/23。
- `test_search.py` 现有非法语言用例用 "Cobol"/"cpp"（"cpp" 是 key 非精确名，仍非法），不受影响；补一条 "C++"/"c#" 合法归一用例锁定新行为。

## Risks / Trade-offs

- [P6 关注页分组结构变化：已关注的 C++/Kotlin 仓从"其它语言"组移入新语言组] → 用户可见但语义改善（分组更精确）；无交互变更、无数据变更，交付走查时点出即可。
- [P8 智能搜索 languages 合法值扩 5 个：LLM 意图产出 "C++" 从被丢弃变为生效] → 行为改善方向一致；prompt 语言枚举（`ai.py:447,462`）原写死 6 语言（探仓漏项，2026-10-11 主 agent 裁定属规格内触点），已改为从 `LANGUAGES` 动态拼接——枚举与 search 层归一集合同源，LLM 产出新增五语言才可能生效（回归锁见 `tests/test_ai.py::test_client_intent_prompt_languages_follow_classifier`）。
- [C#/Kotlin/Swift 原无徽标点色，落 `_DEFAULT_LANG_COLOR` 灰点] → 已拍板（2026-10-11）：`LANG_COLORS` 必补三色（C# #178600、Kotlin #A97BFF、Swift #F05138，GitHub Linguist 官方色口径），落点 `app/web/routes.py:106-119`。
- [边栏 12 项语言组变长，窄屏/移动端边栏更长] → 边栏既有 sticky＋可收起形态不变；22 榜 SSR 体积上升（单榜整页 §13.1 已把默认页收窄为单榜，全量入口 board=all 保留），体积增量 ≈ 5 榜 × 50 行，可接受。

## Migration Plan

1. 代码部署 → `systemctl restart radar`（无 schema 变更，启动无副作用）；
2. 手动跑一次 `precompute_boards`（真实库）立即使 22 榜生效，不等次日 job；
3. A 级预演：真实库验证 11 语言榜成形、各榜行数合理（C++ 榜应有数十行）＋真实页面截图（边栏 12 语言项、cpp/kotlin 单榜直达、P6 分组）交付本人走查。
回滚：代码回滚重启＋再跑一次 precompute_boards 即恢复 17 榜，无数据残留。

## Open Questions

（两条均已拍板闭环，留痕如下——2026-10-11 本人拍板）

- ~~C#/Kotlin/Swift 是否补徽标点色~~ → **已裁定（2026-10-11）：必做**。`LANG_COLORS` 补 C# `#178600`、Kotlin `#A97BFF`、Swift `#F05138`（GitHub Linguist 官方色口径），落点 `app/web/routes.py:106-119`；tasks 1.5 列为必做项。
- ~~产品层文档同步范围~~ → **已裁定（2026-10-11）：除《示意图.html》外全部回写旧数字**——《需求清单》（S1/成功标准"17 个榜"）、《功能闭环清单》功能 1"语言榜 7 块"、《共识文档》§6、《交互流程说明》、《交互流程/12、13》专章均按 §1 口径回写。《示意图.html》为视觉冻结物本次不动，记为**已知漂移**（示意图内"17 榜/语言 7 项"表述与实现不符），留版本复盘处理；tasks 4.3/4.4 已按此更新。**《原型-T021.html》同为历史视觉冻结物（残留"语言 7 项"表述），按示意图同口径并入已知漂移登记、本次不动**（评审 F3-2 补录）。
