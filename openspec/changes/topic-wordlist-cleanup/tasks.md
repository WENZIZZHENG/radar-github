# 任务清单：主题词表清理（topic-wordlist-cleanup）

> 流程口径（项目 SOP）：词表为封板物（决策 12）——proposal.md 逐词 diff 表经本人过目封板后才动 topics.yaml；零页面/交互变化，免示意图与《交互流程说明》更新；触核心链路（榜单计算 → 展示）口径变更，A 级预演（真实库字面预演）＋独立评审子 agent。design.md 两条 Open Questions 已于 2026-10-11 本人拍板收口（删 `editor`、接受"其他"榜膨胀），已回写四件产物。

## 1. 封板物同批更新（topics.yaml＋附录＋测试，缺一 verify 红）

- [x] 1.1 本人封板确认 proposal.md 逐词 diff 表（删 19 词、增 1 词）——验证：本人在会话中明确确认（2026-10-11"没问题，开干"）
- [x] 1.2 `config/topics.yaml`：按 diff 表删 19 词（security `ssr`；devops `ci`/`pipeline`/`metrics`；backend `docker`/`nodejs`/`api`/`server`/`streaming`/`real-time`/`realtime`；frontend `design-system`/`webapp`/`web-application`；ai `agent`/`agents`；data `knowledge-base`/`visualization`；devtools `editor`），devops 增 `continuous-integration`（排在 continuous-delivery 旁）；头注释补"2026-10-11 清理封板（410→392）"；主题顺序/label/行内数组形态不动——验证：`python -c "from app.classify import load_topics; from app.config import BASE_DIR; t=load_topics(BASE_DIR/'config'/'topics.yaml'); print({k: len(v['words']) for k,v in t.items()})"` 输出 ai 122 / frontend 46 / backend 54 / data 36 / devops 21 / devtools 35 / security 41 / game 19 / automation 18
- [x] 1.3 `docs/sop/架构决策记录.md` 附录：yaml 块与 topics.yaml 一字同步；收词沿革补一条 2026-10-11 清理记录（−19/+1，410→392，关联本 change）——验证：附录 yaml 块与 topics.yaml 机器 diff 为空
- [x] 1.4 `tests/test_classify.py`：3 个依赖 `docker` 的用例（多榜重复/结果顺序/防御性归一）改用保留词 `kubernetes`，断言结构与锁定行为不变；词数锁定断言更新为 1.2 的新分布——验证：`pytest tests/test_classify.py` 绿

## 2. 统一验证与真实库预演

- [x] 2.1 统一验证入口 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1` 一次跑绿（ruff + pytest 全量）
- [x] 2.2 A 级预演（本地真实库 data/radar.db 字面执行）：手动跑一轮 precompute_boards（命令见 design.md 决策 5），摘要落盘；记录各主题榜行数变化与"其他"榜增量；抽查典型仓归属——nuxt/dioxus 不再进安全榜、chinese-poetry 不再进 DevOps 榜、donnemartin/system-design-primer 不再进前端榜、dokploy/huginn 不再进 AI 榜、OpenCut/AFFiNE 不再进开发者工具榜（microsoft/vscode 经 `electron` 仍留前端/UI 榜）——验证：预计算摘要＋抽查结果写入复验交付记录

## 3. 评审与交付

- [x] 3.1 独立评审子 agent（核心链路口径变更），中/高 findings 清零（最多三轮）——验证：评审输出 findings 分级记录（agent-735：0 高/0 中/4 低，F3-1/F3-2 已补录，F3-3 随预演证据落盘，F3-4 留痕不修）
- [ ] 3.2 规范复验步骤卡＋预演产物（预计算摘要、榜单行数变化、典型仓抽查）交付本人走查——验证：本人走查确认
- [ ] 3.3 部署生产并刷新榜单：更新词表 → `systemctl restart radar`（先重启，重建页面层词表缓存）→ 手动跑预计算（docs/dev-commands.md 命令形态）→ 冒烟抽查主题榜成员——验证：生产主题榜抽查结果符合新口径

## Workflow follow-up

- 本人走查通过后立即当场归档：`openspec archive topic-wordlist-cleanup`；任务拆解表主表行＋详情卡当场搬归档。
- 归档后核验：`openspec validate --all` 与统一验证入口跑绿；文档按 §5 节奏攒批提交。
