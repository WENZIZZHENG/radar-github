# 任务清单：智能搜索追问与扩量（search-follow-up）

> 流程口径（项目 SOP）：任务 1.1《交互流程说明》已确认（§17 v2.8，2026-08-19 grilling 共识本人拍板后直接实施）；改用户可见口径＋搜索核心链路＋外部 API 接线 → A 级预演；实施派 DeepSeek，评审派 k3。

## 1. 文字层与规格

- [x] 1.1 更新《交互流程说明》§17：追问形态（单输入框复用/意图合并/搜空保留上一轮）、数量 10→20、候选 30→50、成本口径 → 本人确认（2026-08-19 §17 v2.8 已确认）
- [x] 1.2 本 change 四件产物（proposal/design/specs/tasks）＋ `openspec validate --strict` 过

## 2. 检索链路（app/search.py、app/ai.py）

- [x] 2.1 `app/search.py`：`RESULT_LIMIT` 10→20、`POOL_TOP_N` 30→50；模块 docstring 口径段同步
- [x] 2.2 `app/ai.py`：意图理解支持追问合并——`understand_intent` 增可选旧意图参数（或等价新方法），prompt 携旧意图 JSON＋新输入产出合并后新意图（同一 JSON 契约）；非法 JSON 退化为本轮新输入单关键词（不沿用旧意图）
- [x] 2.3 `app/search.py`：`run_search` 增可选 `prev_intent` 参数——有效则走合并链路，缺失/非法按首搜；合并后意图经 `_build_intent` 同一归一/校验路径
- [x] 2.4 精选 prompt 上限文案同步 20 条；`_github_query` 运算符预算口径不变（关键词截前 4、语言截前 2）
- [x] 2.5 filters 白名单（design 决策 6）：Intent 增 `filters`（仅 `created_within_days`/`min_stars`，逐字段校验白名单与类型，非法丢弃记 WARNING）与 `unsupported`（仅展示）；`_build_intent` 归一路径同步；ai.py 意图理解/合并 prompt 教 LLM 产出 filters 与 unsupported（白名单外条件 MUST 进 unsupported，不塞关键词）
- [x] 2.6 召回与补搜过滤：recall_candidates 叠加 filters（`created_within_days` → github_created_at 非 NULL 且距今 <N 天；`min_stars` → 最新快照星数 ≥N）；`_github_query` 追加限定词（`created:>YYYY-MM-DD`/`stars:>=N`，不占运算符预算）

## 3. 页面与接线（app/web/）

- [x] 3.1 `routes.py`：POST /search 增 `prev_intent`/`prev_results` 表单字段解析（JSON 非法丢弃按首搜，不报错）；追问搜空时用 `prev_results` 摘要重渲染上一轮结果区＋新旧意图对比，不额外调 LLM
- [x] 3.2 `search.html`：结果页表单带 `prev_intent`/`prev_results` 隐藏字段（当前轮意图与结果摘要注入）；追问搜空时渲染对比区与上一轮结果区（标注"上一轮结果"）；意图透明行显示当前生效意图
- [x] 3.3 `radar.css`：意图对比区/上一轮结果区样式（复用既有视觉体系，无新组件）
- [x] 3.4 元信息行与相关文案中"10 条"口径同步为 20 条
- [x] 3.5 意图透明行扩展：filters 生效段（"创建：近 1 年内"／"星数 ≥5000"形态）＋unsupported 段（"暂不支持：…"）；prev_intent 隐藏字段序列化含 filters/unsupported（k3 评审 F2-1 pill 视觉回退已修：Intent 抽 filters_text 单点、模板分段渲染 pill）

## 4. 测试

- [x] 4.1 意图合并：旧意图＋新输入产出合并意图、合并非法 JSON 退化为本轮新输入单关键词（不沿用旧意图）
- [x] 4.2 数量：精选至多 20/候选粗排 50/池外补足 20/不足不凑数
- [x] 4.3 prev_intent 缺失/非法按首搜；追问搜空 prev_results 重渲染（含缺失/非法退化）；不发生额外 LLM 调用（mock 计数断言）
- [x] 4.4 既有用例适配（RESULT_LIMIT/POOL_TOP_N 相关断言）；`powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1` 一次跑绿（384 passed：基线 376＋新增 8，含 k3 评审补位的截断用例）
- [x] 4.5 filters：白名单校验（外字段/类型非法丢弃记 WARNING）、召回 SQL 两条件过滤（含 github_created_at NULL 不进）、补搜限定词拼接、unsupported 仅展示不影响检索、追问合并 filters 覆盖语义、意图行 filters/unsupported 渲染（verify.ps1 394 passed 一次跑绿）

## 5. 评审与交付

- [x] 5.1 k3 独立评审，中/高 findings 清零（最多三轮）（初审零 F1/F2，F3×4——三条已修一条裁定留痕；增量复查零 findings）
- [x] 5.2 部署生产 + A 级预演（真实 DeepSeek 跑追问链路：首搜→追问收窄→追问换语言→追问搜空保留上一轮）+ 复验步骤卡 → 本人走查（A 级预演已在本地真实环境完成，搜空分支真实环境未触发、单测覆盖留痕；**2026-08-19 已部署生产：sha256 两端一致、服务 active、生产真实 POST filters 冒烟通过——"我要做爬虫，要创建时间1年内的" 9.1s 出 20 条、意图行"创建：近 1 年内"生效；本次免复验步骤卡系本人拍板，明日本人验证**）
- [x] 5.3 通过后当场归档（任务拆解表 + openspec archive），文档按 §5 节奏提交
