# 设计：本地 AI 导出/回填通道（local-ai-relay）

## Context

- 生产 AI 段已停用（`AI_ENABLED=0`，2026-09-12），但文本需求节奏不变：周榜每周全量重生约 830 条、每日自然新增约 120~190 条（`data/jobs.log` 实测）。
- 现有文本写入路径四条：① 每日 ensure 的 AI 段（停用中）；② 手动批量补缺 worker（`refresh=False`，只补缺）；③ 手动单仓"生成/重新生成"（`POST /api/recommend`，强制覆盖）；④ 采集层简介变更 `DELETE` 该仓全部维度文本。
- prompt 构造现位于 `DeepSeekClient.recommend / summarize / translate` 三个方法内部（system 字符串 + user 行拼装），README 由 `_ReadmeState` 拉取并按 `AI_README_HEAD_CHARS`（生产 3000）截断。
- 判定逻辑集中于 `_scope_sets`（S 全集）与 `recommend_missing`（缺失/窗口/sha 比对）。
- `recommendations` 表当前无法区分文本来源；页面按 `(dimension, period_label)` 精确取文本，没有"当前期次的文本"就显示为空。
- 外部约束：本地工具（WorkBuddy 等）无 API，只能整段粘贴、整段复制。

## Goals / Non-Goals

**Goals:**

- 提供"导出待生成任务 → 本地生成 → 回填结果"两个接口，判定口径与每日 ensure 逐条一致（范围、缺失、窗口、README 输入）。
- 人工生成的行不被任何自动路径覆盖；人工行与自动行可区分、可审计。
- 全流程可重跑、可幂等、可部分失败（一批里好坏条互不牵连）。

**Non-Goals:**

- 不改生成节奏与范围（周榜维持每周重生、S 全集不变）——本人 2026-09-13 拍板。
- 不做页面面板（v1 纯接口，curl/脚本消费）；不做任务认领锁与进度看板。
- 不恢复 `AI_ENABLED`，不引入任何新依赖、不改每日跑批时序。

## Decisions

### 1. 判定与 prompt 构造复用，不复制第二套口径

导出直接复用 `_scope_sets` 与 `recommend_missing` 的判定序列（translate → week → quarter → total → summary，顺序与每日跑批一致），把现位于 `DeepSeekClient` 内的 prompt 构造抽为模块级纯函数（`build_recommend_prompt` / `build_summary_prompt` / `build_translate_prompt`），AI 客户端与导出共用同一份实现。

- 备选：导出侧重写一套判定与 prompt → 否。两套口径必然随时间漂移，"和线上 AI 生成的一模一样"是本变更的核心承诺。
- 备选：导出直接调用 `recommend_missing` 的 dry-run 变体 → 否。该函数耦合了写库与计数语义，抽取纯函数更干净，且不影响既有路径。

### 2. 无状态导出 + 按 (repo, kind, period_label) 定位回填

导出不写任何服务端状态；回填按 `repo` + `kind` + `period_label` 匹配目标行，并按库内**当前**状态做条件写入。

- 备选：服务端任务表（登记 `task_id` → 身份）→ 否。引入生命周期、清理与"导出了没回填"的悬挂态，而写入安全性本来就由回填时的条件判断保证，任务表提供不了额外保证。
- 备选：base64 批次令牌携带导出时刻身份与 sha → 否。需要人多粘一大串内容；sha 的替代方案见下条。

### 3. total/summary 的 README 指纹取回填时刻重新拉取

回填时对该仓库重取 README blob sha 作为新行指纹；拉取失败写 NULL。

- 理由：指纹不该经过本地工具的手（40 位十六进制串被抄错就静默失效）；重取成本是一次 GitHub 调用。
- 已知偏差：导出与回填之间 README 又发生变更时，指纹记的是变更后版本 → 该次变更延到下一个窗口日才被发现（可接受，属探测能力边界）。
- NULL 分支：下次窗口日会被判为变更而重生一次（自愈），不阻塞本次写入。
- 更正（评审 F3-4）：指纹只对 `source='ai'` 的行有意义——人工行在窗口日被**直接跳过**（不拉 README、不比 sha），故人工行的 `readme_sha` 是死数据，上面"写 NULL → 下次窗口日自愈"这句对人工行**不成立**；人工行只由用户显式回填或手动单仓重生更新。

### 4. 人工行保护用 `source` 列

`recommendations` 增加 `source TEXT NOT NULL DEFAULT 'ai'`；回填写 `manual`，自动路径写 `ai`；窗口日重生遇到 `manual` 行直接跳过（不拉 README、不比 sha）。

- 备选：单独一张"人工行"表 → 否。查询与页面读取要多一次 join，收益为零。
- 备选：不加保护 → 否。本人 2026-09-13 明确认可保护（否则人工文本会在 1 号/15 号被自动重生覆盖）。
- 迁移用 `ALTER TABLE ADD COLUMN`（SQLite 的 O(1) 元数据操作）；旧代码不认识该列也能正常读写（默认值生效），回滚安全。

### 5. 默认只补缺失，`overwrite=true` 才覆盖

与既有语义对齐（批量=只补缺、单个=强制重生），不引入第三种覆盖语义；覆盖是显式动作，防手滑。

- 同批内重复键：按顺序处理，后到者因"已存在"计入 skipped（与 overwrite 语义一致）。

### 6. 期次绑定校验，跨期直接拒绝

`week` / `quarter` 任务必须与当前期次标签一致，`total` / `summary` 固定 `all`。跨期回填写出的行，页面按当前期次取文本时看不到（等于白干），故直接拒绝并提示重新导出。

### 7. 回填文本最小清洗

去首尾空白、成对包裹引号、"推荐理由：/推荐语：/译文："类前缀；只去壳不改正文。本地工具未必遵守我们的 prompt 约束，清洗保证页面口径一致；清洗后为空则按格式错误拒绝。

### 8. 接口形态与上限

`GET /api/local-ai/tasks`（只读）与 `POST /api/local-ai/fill`（写入）复用 Caddy Basic Auth 一层，不新增鉴权面。导出 `limit` 默认 20、上限 50；回填单批条目上限 200、请求体上限 1MB，超限整批拒绝。JSON 解析复用既有容错思路（剥 markdown 围栏 + 大括号截取）。

### 9. v1 不做页面面板

本人明确"只是新增接口，本地调用回传"；curl 用法写入 `docs/dev-commands.md`。面板属新交互元素，另开任务并单独走示意图分档。

## Risks / Trade-offs

- [手工量约 830 条/周，接口不解决量] → 本人知情；支持分批导出、按 `kind` 过滤，可只导重点类别。量的问题由产品口径另议（本变更不改节奏）。
- [期次过期导致整批失败] → 错误明细逐条给出原因；作业单首行显著标注期次与导出时刻。
- [本地工具输出格式不稳（带前缀/引号/多余解释）] → 最小清洗 + JSON 容错解析 + 逐条 errors；重贴一批的成本只是时间。
- [写入路径从四条变五条] → 语义在规格中钉死（默认补缺、显式覆盖、期次校验、来源标记）；`source` 列可审计。
- [total/summary 指纹的导出—回填间隙偏差] → 见决策 3，偏差有界且自愈。
- [窗口日 README 探测的推进性] → 探测从 `total_names`/`all_names` 头部开始且服务端不记状态，**两段（total/summary）各持一份预算**（各为 `min(max(limit×8, 200), 500)`；共用会让 total 段吃光预算、summary 段整轮零判定——评审 F2-1）。**另一条触发路径是 `limit`**：total 段先凑满 `limit` 会让 summary 段整轮不跑（评审 F2-2'）——故两段因 `limit` 提前结束时各补一次"只扫名不探测"的尾扫，把本该探测而未判定的候选并入 `probe_skipped`，使 `probe_skipped>0` 与作业单提示对两条路径都成立。实测单次约 200~400 次 GitHub 调用（≈单段预算，跨段 README 缓存命中；理论上界 800）。推进靠"回填后该行成人工行、不再占探测预算"逐批前进。**`probe_skipped` 是告警值不是进度**：探测无服务端状态，同一窗口日重复导出不会使其清零，生产规模下它恒 >0（实测候选合计 1413 vs 单请求上限 800，恒有下界）——故窗口日的实际收工判据是"本次导出没有任何 total/summary 重生任务"；真正的进度计需服务端游标，属另开任务。若将来嫌慢，可做"服务端探测游标"或"窗口日全量比对"，本变更不做。
- [导出在请求内同步做重活] → 实测 limit=20 约 7.6 秒（成本在 `_scope_sets` 的 3×`compute_boards` ＋全表读），窗口日再叠加 README 拉取；单人自用、批量动作场景下"导出期间页面短暂无响应"可接受，不做异步化重构（需拆 CPU 段与 async IO 段），留待有实际体感时另开任务（评审 F3-1，接受不修）。
- [回填 1MB 上限为"读完再拒"] → `await request.body()` 先整段进内存再判长度；既有写端点同款，Caddy＋Basic Auth＋单人使用下风险低（评审 F3-7，接受不修）。
- [生产新增写接口] → 仅 Basic Auth 可达、无匿名面；单人使用；请求体与批条目双上限。
- [迁移出错] → `ALTER TABLE ADD COLUMN` 幂等（先查 `PRAGMA table_info`），与既有两条 recommendations 迁移并列在 `init_db` 迁移链；失败即回滚部署（旧代码不依赖新列）。

## Migration Plan

1. 本地 `tools/verify.ps1` 一次跑绿（ruff + pytest，含新增用例）。
2. 部署按 `docs/dev-commands.md` §4 流程（`tar` + `scp` + `install` + 重启）；`.env.prod` 覆盖时保持 `AI_ENABLED=0`（不恢复自动 AI）。
3. 部署后回环冒烟 + 生产 `curl` 小批次真实闭环（导出 `limit=3` → 本地生成 → 回填 → 页面核对渲染），属 A 级预演（含外部接口接线与写入）。
4. 回滚：回滚旧代码即可；`source` 列对旧代码无影响（默认值 `ai`）。

## Open Questions

- 面板是否值得做（v2 议题：涉及新交互元素，需单独走视觉分档确认）。
- 导出单批上限 50 是否匹配本地工具的最佳批次大小（先按 20 默认，实测后可调）。
