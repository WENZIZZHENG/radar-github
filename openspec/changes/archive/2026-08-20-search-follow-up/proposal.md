# 提案：智能搜索追问与扩量（search-follow-up）

## Why

智能搜索（smart-search，已归档）是单发检索：搜完想进一步收窄（"只要异步的""换成 Go 的"）只能重新组织一句完整描述重搜，且每次至多 10 条在候选充足时偏紧。2026-08-19 grilling 共识（Q3~Q6 逐条拍板）：支持追问式筛选＋结果 10→20。

## What Changes

- **多轮追问**：结果页同一输入框继续输入补充/修正条件 → 前端把上一轮意图（JSON）随新输入回传 → LLM 合并产出新意图 → 重跑检索链路，结果整体替换；意图透明行显示当前生效的合并意图。
- **追问上下文无状态**：不建会话表、不落库；刷新即清零；上一轮意图回传缺失/非法按首搜处理。
- **追问搜空不断档**：新一轮池内＋池外全空时，如实说明＋新旧意图对比，上一轮结果保留在下方（由前端随表单回传的上一轮结果摘要重渲染，不额外调 LLM）。
- **数量 10→20**：精选至多 20 条、候选粗排池 30→50、池外补搜补足 20；单次 DeepSeek token 消耗约为原口径两倍（本人拍板接受）。
- **结构化筛选条件白名单（filters，2026-08-19 本人拍板扩入）**：意图契约扩可选 filters——第一版白名单 `created_within_days`（创建时间，如"1 年内"）＋`min_stars`（星数下限）；池内 SQL 真过滤、池外映射 GitHub 限定词（不占布尔运算符预算）；白名单外条件进仅展示的 `unsupported` 回显（"暂不支持：…"），不硬塞关键词。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `smart-search`: 新增追问链路（意图合并、无状态回传、搜空保留上一轮）；精选/补搜数量 10→20、候选粗排池 30→50；意图理解接受可选旧意图输入。

## Impact

- **代码**：`app/search.py`（常量 10→20/30→50、run_search 接 prev_intent）、`app/ai.py`（意图理解支持旧意图合并）、`app/web/routes.py`（POST /search 增 prev_intent/prev_results 表单字段、追问搜空重渲染）、`app/web/templates/search.html`（隐藏字段回传、新旧意图对比区、上一轮结果区）、`app/web/static/radar.css`（对比区样式）、`tests/test_search.py`/`tests/test_web.py`。
- **外部依赖**：无新增；DeepSeek/GitHub Search 均为既有接入，调用次数口径不变（单次 1~3 次 DeepSeek），payload 变大。
- **降级姿态**：意图合并返回非法 JSON → 以本轮新输入单关键词退化（不沿用旧意图）；其余降级口径沿用 smart-search 不变。
- **项目 SOP**：改用户可见口径（10→20）＋搜索核心链路 → OpenSpec change 先行；A 级预演（真实 DeepSeek 跑追问链路）；视觉为既有组件复用，免示意图档。
