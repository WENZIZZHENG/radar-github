# 提案：智能搜索（smart-search）

## Why

雷达现在是"看榜"工具：用户想知道"做爬虫/桌面端/Java 项目有什么值得参考的项目"时，只能逐榜翻找。池内已有 3024 仓（≥1000 星）的描述、语言、topics、增星数据，加既有 DeepSeek 基建（翻译/推荐语/概要），具备做"自然语言意图 → 项目推荐"的全部原料。

## What Changes

- **新增独立搜索页**（导航栏加入口）：大输入框 + 结果列表；结果行复用榜单 `_row.html` 行组件，每行多一句 LLM 推荐理由。
- **检索链路**（LLM 理解意图、本地检索、LLM 精选）：
  1. 用户自然语言输入 → DeepSeek 理解，产出**多个关键词 × 多个语言 × 主题词**（不锁单个）；
  2. 池内按关键词/语言召回候选，按命中数+星数粗排取约前 30；
  3. DeepSeek 按匹配度精选 **10 个**并各写一句推荐理由；不足 10 个有几个列几个，不凑数；池内+池外一个都没有时如实说明原因，不硬编。
- **池外实时补搜**：池内捞不满 10 个时，用同组关键词实时调 GitHub Search API 补足；池外结果带"池外"徽标，无增星/译文，展示 GitHub 原始描述。
- **关注闭环**：池外结果行尾带关注星，点击走既有 `ingest_followed_repo` 动态入池——搜索成为池子的有机扩充入口。
- **成本**：每次搜索 1~3 次 DeepSeek 调用 + 至多几次 GitHub Search（限速 30 次/分，单人使用够用）。

## Capabilities

### New Capabilities

- `smart-search`: 搜索页与导航入口；LLM 意图理解（多关键词×多语言×主题词）；池内候选召回与粗排；LLM 精选 10 条与推荐理由生成；空结果如实说明；池外实时补搜与"池外"标记；池外结果一键关注入池。

### Modified Capabilities

（无——`openspec/specs/` 尚无既有规格覆盖搜索/关注/导航，本变更为全新能力。）

## Impact

- **代码**：`app/web/routes.py`（搜索页路由 + 搜索 API + 池外关注接线）、`app/web/templates/`（新 search.html，复用 `_row.html`）、`app/ai.py` 或新模块（意图理解/精选 prompt 与调用，复用 DeepSeek 客户端与降级姿态）、`app/collector/github.py`（复用 `search_repositories` 做池外补搜）、池内召回查询（SQLite，描述/topics/语言匹配）。
- **外部依赖**：无新增；DeepSeek API 与 GitHub Search API 均为既有接入。
- **降级姿态**：DeepSeek key 未配置/调用失败 → 搜索页明确提示不可用（fail-loud 给用户，不影响榜单主链路）；GitHub Search 失败 → 池内结果照常返回，池外区标注"补搜失败"。
- **项目 SOP**：新功能+核心链路外延（外部 API 实时接线）→ 先《交互流程说明》文字层确认，A 级预演；视觉为既有组件复用（搜索框+行组件+徽标），免示意图档。
