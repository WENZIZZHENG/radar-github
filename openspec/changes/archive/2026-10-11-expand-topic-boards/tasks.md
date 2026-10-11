# 任务清单：主题榜扩容（expand-topic-boards）

> 流程口径（项目 SOP）：词表为封板物（决策 12）——proposal.md 逐词 diff 表经本人过目封板后才动 topics.yaml；零页面/交互/视觉元素变化（主题榜无边栏点色），免示意图与《交互流程说明》流程更新（仅数量表述回写）；触核心链路（榜单计算 → 展示）口径变更，A 级预演（真实库字面预演）＋独立评审子 agent。前置依赖：topic-wordlist-cleanup 的词表基线（9 主题 392 分布）已在工作区落地；本 change 归档排在其之后（design.md 决策 9）。

## 1. 封板物同批更新（topics.yaml＋附录＋测试，缺一 verify 红）

- [x] 1.1 本人封板确认 proposal.md 逐词 diff 表（新增 6 主题 70 词位：learning 16 / mobile 8 / embedded 12 / media 14 / selfhosted 6 / blockchain 14，含 mobile 与 frontend 共有 5 词双榜破例）——验证：本人在会话中明确确认（2026-10-11"没问题，开干"）
- [x] 1.2 `config/topics.yaml`：automation 尾部追加 6 主题块（顺序 learning→mobile→embedded→media→selfhosted→blockchain；label 依次为：学习资源、移动、嵌入式/硬件、音视频/媒体、自托管/家居、区块链/金融；词表逐字按裁定 1，行内流式数组、主力词在前语义相近相邻）；头注释追加"2026-10-11 扩展封板（+6 主题 70 词位，392→462 词位/457 唯一词）"；现有 9 主题顺序/label/词表不动——验证：`python -c "from app.classify import load_topics; from app.config import BASE_DIR; t=load_topics(BASE_DIR/'config'/'topics.yaml'); print({k: len(v['words']) for k,v in t.items()})"` 输出 ai 122 / frontend 46 / backend 54 / data 36 / devops 21 / devtools 35 / security 41 / game 19 / automation 18 / learning 16 / mobile 8 / embedded 12 / media 14 / selfhosted 6 / blockchain 14
- [x] 1.3 `docs/sop/架构决策记录.md` 附录：yaml 块与 topics.yaml 一字同步；收词沿革补一条 2026-10-11 扩展记录（+6 主题 70 词位，392→462 词位/457 唯一词，关联本 change）——验证：附录 yaml 块与 topics.yaml 机器 diff 为空
- [x] 1.4 `tests/test_classify.py`：词数锁断言（:165-178）更新为 1.2 新分布；主题顺序锁（:135-147）更新为 15 项新序列；label 锁（:150-162）增 6 个新 label；新增每主题至少一条精确命中用例（leetcode→learning、android→mobile、esp32→embedded、jellyfin→media、home-assistant→selfhosted、solidity→blockchain）＋一条单复数归一用例（cheatsheets→learning、androids→mobile、fpgas→embedded、podcasts→media、nextclouds→selfhosted、wallets→blockchain；`algorithms` topics 由词条 `algorithm` 尾"s"归一命中 learning——词形修正的回归锁）＋一条 y→ies 变体用例（topics `cryptocurrencies` 命中 blockchain，词条 `cryptocurrencies` 显式收录）；新增双榜断言 `classify_topics(["flutter"], table) == ["frontend", "mobile"]`——验证：`pytest tests/test_classify.py` 绿

## 2. 注释与计数常量同步（零逻辑改动）

- [x] 2.1 代码注释数量表述刷新：`app/report.py:343,346`（"封板词表 9 主题 → 共 22 张榜"→15 主题/28）；`app/web/routes.py:642,646,732,747,946`（"22 榜"类表述）；`app/schema.sql:94`（"全量 22 榜"）；`app/web/templates/boards.html:60`（"全量 22 榜"）——验证：`git diff` 仅注释/表述变化，无逻辑行改动
- [x] 2.2 `README.md:10` "各 22 张分类榜（11 语言＋其它语言、9 主题＋其他）" → "各 28 张分类榜（11 语言＋其它语言、15 主题＋其他）"；`README.md:11` "三口径 22 榜落 board_cache" → 28——验证：人工核对
- [x] 2.3 测试计数常量：`tests/test_report.py:221-227`（docstring "主题 10 张"→16、`len(boards) == 22`→28）；`tests/test_board_cache.py:143`（22→28）、`:279-282`（`"boards": 22`→28 三处）；`tests/test_web.py` 22/23→28/29（:4-5 docstring、:165-184、:190-202、:831-853、:943-962 含 :953"其余 21 榜"→27、:1049-1107，以实际断言行号为准）——验证：`pytest tests/test_report.py tests/test_board_cache.py tests/test_web.py` 绿
- [x] 2.4 零逻辑改动核对：榜单装配（`app/report.py` 遍历词表）、边栏/白名单（`app/web/routes.py:645-652` 动态派生）无硬编码主题清单残留——验证：grep 复查词表主题 key 在 app/ 下无硬编码

## 3. 统一验证与真实库预演

- [x] 3.1 统一验证入口 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1` 一次跑绿（ruff + pytest 全量）
- [x] 3.2 A 级预演（本地真实库 data/radar.db 字面执行）：手动跑一轮 precompute_boards（命令形态见 topic-wordlist-cleanup design.md 决策 5），摘要落盘；记录 6 个新主题榜行数与"其他"榜瘦身幅度；抽查典型仓归属——system-design-primer/CyC2018-CS-Notes 进学习资源榜、termux 系进移动榜、zephyr 进嵌入式榜、jellyfin 进音视频榜、home-assistant 生态进自托管榜、viem/optimism 进区块链榜——验证：预计算摘要＋抽查结果写入复验交付记录（主 agent 亲自执行，证据落盘 Temp/precompute-evidence-3.txt：三口径 28 榜、6 新榜各满 50 行、零命中 4836→4173 捞回 663 仓、典型仓 classify 判定全对、双榜抽查 zephyr=c＋embedded 正确）

## 4. 评审与交付

- [x] 4.1 独立评审子 agent（核心链路口径变更），中/高 findings 清零（最多三轮）——验证：评审输出 findings 分级记录（agent-735 增量评审：0 高/0 中/1 低；F3-1 test_web.py:178 注释分解数漏刷"主题 10"→16，主 agent 已当场修复）
- [ ] 4.2 规范复验步骤卡＋预演产物（预计算摘要、6 新榜行数、"其他"榜瘦身幅度、典型仓抽查）交付本人走查——验证：本人走查确认
- [x] 4.3 产品层文档回写（验收通过后按 §1 口径，裁定 5 全量范围）：《需求清单》（:38 成功标准"22 个榜"→28）、《功能闭环清单》（:8 功能 1"主题榜 10 块"→16 块）、《共识文档》（:49"主题榜单（10 块）"→16 块）、《交互流程说明》（:10、:19、:54、:58、:138 的 22 榜/主题榜 10 块/10 项表述→28/16）、《交互流程/12-左侧边栏导航与P6标签筛选.md》（:6、:7 主题榜 10 项→16 项、22 榜→28）、《交互流程/13-单榜整页与移动端行布局.md》（:3、:7 全量 22 榜→28）——验证：grep 复查无残留旧数字
- [ ] 4.4 《示意图.html》《原型-T021.html》本次不动（视觉冻结物）：其"主题 10 项/22 榜"表述与实现的差异并入 expand-language-boards 已登记的已知漂移，留版本复盘处理（design.md 决策 8）
- [ ] 4.5 部署生产并刷新榜单：更新词表 → `systemctl restart radar`（先重启，重建页面层词表缓存）→ 手动跑预计算（docs/dev-commands.md 命令形态）→ 冒烟抽查新主题榜成员——验证：生产主题榜抽查结果符合新口径

## Workflow follow-up

- 本人走查通过后立即当场归档：`openspec archive expand-topic-boards`（前置：topic-wordlist-cleanup 与 expand-language-boards 均已归档，归档顺序三链钉死——topic-wordlist-cleanup → expand-language-boards → expand-topic-boards，否则 MODIFIED delta 归档被拒，见 design.md 决策 9/10）；任务拆解表主表行＋详情卡当场搬归档。
- 归档后核验：`openspec validate --all` 与统一验证入口跑绿；文档按 §5 节奏攒批提交。
- ~~起草存疑点待拍板~~（design.md Open Questions）：expand-language-boards delta 的"22 张/主题组 10 张"表述过时问题——已裁定：本 change 增带 language-boards MODIFIED delta（已落 `specs/language-boards/spec.md`），见 design.md 决策 10。
