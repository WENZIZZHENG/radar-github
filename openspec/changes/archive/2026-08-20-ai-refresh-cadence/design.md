# 设计：AI 推荐语/概要重生节奏降本（ai-refresh-cadence）

## Context

家底：`app/ai.py` 中 `recommend_missing` 负责每日 ensure 路径下的三维度推荐语（week/quarter/total）与 AI 概要（summary）生成。当前逻辑：

- `quarter`：`(repo_id, 'quarter', 当季标签)` 缺失或其 `generated_week ≠ 当周` → REPLACE（季内每周重生）。
- `total`：`(repo_id, 'total', 'all')` 缺失则生成；refresh=True 时 README blob sha 变化 → REPLACE（README 变更当日重生）。
- `summary`：与 total 同 sha 守卫，README 变更当日重生。
- `week`：每周一全量生成新周标签行（既有行同周跳过）。

成本实测：total 440~670 次/周、summary 623~803 次/周、quarter 96~124 次/周。目标是在不牺牲"缺失行自动补缺"的前提下，把非周榜维度的重生频率降到半月级。

约束：单人使用、S 档；核心链路（每日采集入库 → 榜单计算 → 展示）不能动；不能引入新依赖；不能碰 `data/`；不许改 `tests/conftest.py`。

## Goals / Non-Goals

**Goals:**
- 非窗口日对 quarter/total/summary 既有行零 GitHub API 调用（不拉 README、不比 sha）。
- 窗口日（每月 1 号、15 号）正常执行原有重生判定：quarter 的 generated_week 过期、total/summary 的 README sha 变化。
- 缺失行任何日期都照常补缺生成。
- 周榜与 refresh=False 手动批量路径行为保持不变。

**Non-Goals:**
- 不改推荐语/概要 prompt、输出格式、落库 schema。
- 不新增缓存表/调度器/定时任务。
- 不改 `app/config.py` 中 `AI_README_HEAD_CHARS` 代码默认值（`.env.example` 兜底）。
- 不碰 `data/` 目录与后台写库进程。

## Decisions

### 决策 1：半月窗口判定——每月 1 号、15 号

新增本地 helper `_refresh_window_open(d: date) -> bool`，返回 `d.day in (1, 15)`。用 `now.date()` 判定，不引入新依赖。窗口日保持原重生语义；非窗口日对已有行提前 `continue`，不进入 README 拉取与 sha 比对。

备选：按"自然半月"（1~15 号、16~月底）→ 否决，会模糊触发时机、放大调用量；按"每月仅 1 号"→ 否决，季中推荐语可能过旧。本人拍板 1 号+15 号双窗口。

### 决策 2：quarter 段——窗口日才检查 generated_week

保留 `generated_week ≠ 当周` 作为重生条件，但仅在窗口日执行检查；非窗口日直接跳过已有行。缺失行（`cur is None`）不受窗口限制。

实现：`if cur is not None and (not refresh or not window_open or cur["generated_week"] == week_label): continue`。

### 决策 3：total/summary 段——窗口日才拉 README 比 sha

为严格省 GitHub API，对已有行在非窗口日直接 `continue`，不调用 `_ReadmeState.get`；仅在窗口日拉取并比对 sha。缺失行照常拉取生成。

实现：
```python
cur = existing.get(key)
if cur is not None and (not refresh or not window_open):
    continue
readme_text, sha = await readme_state.get(full_name, stats)
if cur is not None and (sha is None or cur["readme_sha"] == sha):
    continue
```

summary 段在 `if refresh:` 块内，故省略 `not refresh` 检查，但保留 `not window_open` 提前跳过。

### 决策 4：week 段与 refresh=False 路径不变

week 维度维持每周一全量重生（本人拍板）。`refresh=False` 手动批量补缺只走缺失生成逻辑，本来就不触发重生，无需额外改动；窗口判定也不影响其补缺行为。

### 决策 5：README 截断上限 3000 入 `.env.example`

Q1 已由主 agent 在配置层完成；本次仅把 `.env.example` 第 34 行从注释态 `# AI_README_HEAD_CHARS=8000` 改为正式项 `AI_README_HEAD_CHARS=3000`，并更新注释说明降本后现行口径。`app/config.py` 默认值 8000 保留，由 env 显式配置兜底。

## Risks / Trade-offs

- [推荐语文本陈旧窗口期变长] → 可接受：总星/季榜/概要文本对"每日最新"不敏感，半月刷新足够支撑单人使用；周榜保持每周刷新承担"热点感"。
- [1 号/15 号集中触发造成当日调用量峰值] → 可接受：峰值约为原日调用量的 2 倍（两窗口日承担原 7~15 天重生量），仍在 DeepSeek 常规额度内；如未来继续缩窗，再议。
- [非窗口日 README 被更新后页面展示旧推荐语/概要] → 按设计接受：窗口日才会自愈更新。
- [测试需覆盖窗口日/非窗口日/周榜/手动路径] → 通过新增/修改 `tests/test_ai.py` 用例覆盖。
- [窗口日判定按 UTC 日期，实际在北京时间 2 号/16 号 05:00 触发] → 可接受：每月稳定两次、不漏窗口，仅语义偏移，spec 已注明。
- [窗口日当天进程停机超 misfire_grace_time（1 小时）不补跑] → 可接受：重生顺延至下个窗口日（最多晚 14 天），S 档单人使用无感；采集主流程不受影响。

## Migration Plan

纯逻辑变更：无 schema 变更、无数据迁移。部署后次日 ensure 按新窗口执行。回滚=还原 `app/ai.py` 与 `.env.example`。

## Open Questions

- 窗口日若遇 GitHub API 拉取失败/停拉，当日不会重生 → 按 F2-1 守卫保留旧指纹，下次窗口日再比对（决策 3 已覆盖）。
