# 提案：主题词表清理（topic-wordlist-cleanup）

## Why

生产实测：上榜仓库 630 个中存在系统性错分，根因是词义撞车＋词条过宽——`ssr`（安全榜指 ShadowsocksR）撞 server-side rendering 把 nuxt/dioxus 拉进安全榜；`ci` 把 chinese-poetry 拉进 DevOps 榜；`design-system`+`webapp` 把 donnemartin/system-design-primer 拉进前端榜；`agent`/`agents` 把 coder/dokploy/huginn 拉进 AI 榜。主题榜是核心链路（每日采集 → 榜单计算 → 榜单展示）的用户可见输出口径，错分直接稀释主题榜信号。2026-10-11 本人拍板逐词清理清单（见下表，封板物，本人过目后才生效）。

## What Changes

- **只改词表数据，不改匹配逻辑**：`config/topics.yaml` 删 19 词、增 1 词（410 → 392）；`app/classify.py` 的 `classify_topics` 匹配逻辑零改动（无权重机制、无代码变更）。
- **逐词 diff 表**（封板依据；含 2026-10-11 补充裁定：devtools 删 `editor`）：

| 主题 | 词条 | 动作 | 理由 | 主要受影响仓库例子 |
| --- | --- | --- | --- | --- |
| security | `ssr` | 删除 | 词义撞车：安全榜语义指 ShadowsocksR，但撞 server-side rendering；翻墙词群还有 v2ray/vless/vmess/trojan/clash/mihomo/hysteria2/gfw 兜底 | nuxt、dioxus（前端框架被拉进安全榜） |
| devops | `ci` | 删除 | 词义撞车：两字母缩写泛词，保留 ci-cd/cicd/continuous-delivery 并另补 continuous-integration | chinese-poetry（中华诗词库被拉进 DevOps 榜） |
| devops | `pipeline` | 删除 | 数据管道撞车：ETL/数据类仓 topics 常带 pipeline | 数据管道类仓（泛词，无单一典型） |
| devops | `metrics` | 删除 | 撞车："指标"语义超出监控（ML 评估、名单统计等） | supervision、dns-blocklists |
| devops | `continuous-integration` | 新增 | 补 `ci` 删除后的精确词，与既有 continuous-delivery 成对 | ——（新增词，无既有受影响仓） |
| devtools | `editor` | 删除 | "编辑器"泛词撞车：视频剪辑/文档编辑/3D 编辑器类噪声大于收益（2026-10-11 补充裁定） | OpenCut（视频剪辑）、AFFiNE（文档编辑器）、pascalorg（3D 编辑器）；microsoft/vscode 经 `electron` 兜底仍留前端/UI 榜 |
| backend | `docker` | 删除 | 部署泛词：自托管应用、编辑器、游戏等各类仓都带，把非后端仓拉进后端榜 | 自托管应用类仓（泛词，无单一典型） |
| backend | `nodejs` | 删除 | 运行时泛词：前端工具链、桌面应用同样基于 Node | 工具/桌面应用类仓 |
| backend | `api` | 删除 | 泛词：凡提供 API 的仓都带，与后端无必然归属 | 各类 API 仓 |
| backend | `server` | 删除 | 泛词：各类服务端/自托管应用都带 | 各类 server 仓 |
| backend | `streaming` | 删除 | 泛词：流媒体应用（视频/音频）撞数据流后端语义 | 流媒体应用类仓 |
| backend | `real-time` | 删除 | 泛词：实时应用（协作/游戏/聊天）泛用 | 实时应用类仓 |
| backend | `realtime` | 删除 | 同上（连写变体） | 同上 |
| frontend | `design-system` | 删除 | 词义撞车：撞 system-design（系统设计学习资源） | donnemartin/system-design-primer |
| frontend | `webapp` | 删除 | web 应用泛词：任意 web 应用都带，与前端技术无必然归属 | donnemartin/system-design-primer（同上仓叠加命中） |
| frontend | `web-application` | 删除 | 同上（长形态变体） | 各类 web 应用仓 |
| ai | `agent` | 删除 | 泛词：撞自动化/编排/游戏 agent 等非 AI 语义 | coder、dokploy、huginn |
| ai | `agents` | 删除 | 同上（复数形态，显式收录的词条，与单复数归一规则无关） | coder、dokploy、huginn（同上） |
| data | `knowledge-base` | 删除 | 知识库应用 ≠ 数据工程：撞笔记/文档/知识管理应用 | 知识库应用类仓 |
| data | `visualization` | 删除 | 泛词：前端图表/可视化应用撞数据工程；保留精确词 `data-visualization` | 可视化应用类仓 |

- **明确保留的裁定**（本次不动，防清理过度）：devops 保留 `monitoring`（核心词，worldmonitor 级噪声容忍）；ai 保留 `ai-agent`/`ai-agents` 等精确词与 `ai`（主题本名，ClickHouse 级营销噪声容忍）；frontend 保留 react/vue/angular 等框架词（前端榜骨干，influxdb/supabase 类噪声属结构性容忍）与 gui/desktop/dashboard（词表设计本意即把桌面/移动 UI 应用归入前端/UI 主题）；backend 保留 kubernetes/containers/k8s/cloud-native 等云原生词与 rest-api/graphql/grpc 等协议词；devtools 保留 `compiler`（rust-lang/rust 等真编译器需要它；nextjs/svelte 经此词进开发者工具榜属容忍噪声）；game、automation 两主题不动（CAD/GUI 库归游戏/图形、shell 工具归自动化/脚本，属主题设计语义范围内）。
- **封板物同步**：`docs/sop/架构决策记录.md` 附录主题词表 yaml 块一字同步、收词沿革补一条本次清理记录；`config/topics.yaml` 头注释补封板日期（命中规则不变）；`tests/test_classify.py` 依赖被删词条的用例与词数锁定断言同步更新。
- **生效链路**：改词表后重启服务（页面层 `_topic_table()` 模块级缓存）＋手动跑一次预计算刷新 board_cache（词表不在缓存失效判定内）；不手动跑则次日 05:00 每日任务自动以新词表重算。主题榜成员变化属用户可见口径变更。

## Capabilities

### New Capabilities

- `topic-classification`: 主题分类的行为契约——唯一分类路径（封闭词表精确匹配＋尾 s 归一）、主题榜成员归属口径（命中多主题多榜重复、零命中进"其他"）、词表封板治理（附录与词数锁定断言同步、AI 候选词不直接写词表）、词表变更的生效链路（经预计算/重启生效）。既有 5 个 spec（ai-refresh-cadence、board-hide-tagged、new-project-zone、smart-search、tag-categories）均不覆盖主题分类契约（board-hide-tagged/smart-search 仅引用主题榜存在性与语言词表），本 capability 把此前只在《架构决策记录》决策 3/12 中的契约钉进规格库，并锁定本次清理后的词表口径。

### Modified Capabilities

（无——无任何既有 spec 拥有"主题榜成员由词表决定"这条契约，无既有 requirement 文本被修改；本次变化以新 capability 的 ADDED requirements 承载。）

## Impact

- **代码**：无逻辑改动。`config/topics.yaml` 词表数据（删 19 词、增 1 词、头注释补封板日期）。
- **文档**：`docs/sop/架构决策记录.md` 附录（yaml 块同步＋收词沿革记录）。
- **测试**：`tests/test_classify.py`——3 个使用 `docker` 的用例（多榜重复、结果顺序、防御性归一）换用保留词条；词数锁定断言更新为清理后分布（ai 122 / frontend 46 / backend 54 / data 36 / devops 21 / devtools 35 / security 41 / game 19 / automation 18，总 392）。
- **数据**：无 schema 变更；board_cache 内容随下次预计算刷新。
- **用户可见口径**：9 张主题榜成员变化（被删词条唯一命中的仓掉出对应主题榜，零命中仓进"其他"榜），属用户可见口径变更——逐词 diff 表经本人封板后生效。
- **外部依赖**：无新增。
- **项目 SOP**：改用户可见榜单口径＋触核心链路（榜单计算 → 展示）→ OpenSpec change 先行（本 change）；零页面/交互变化，免示意图与《交互流程说明》更新；A 级预演（真实库手动预计算＋榜单成形核对）；独立评审子 agent（核心链路口径变更）。
