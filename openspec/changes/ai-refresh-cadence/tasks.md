# 任务清单：AI 推荐语/概要重生节奏降本（ai-refresh-cadence）

> 流程口径：改核心链路（每日 ensure）+ 用户可见口径 → OpenSpec change 先建后改；A 级预演；实施派 secondary，评审派 k3。

## 1. OpenSpec 与文档

- [x] 1.1 创建 `openspec/changes/ai-refresh-cadence`：proposal / design / tasks / specs 四件产物
- [x] 1.2 跑通 `openspec validate --strict ai-refresh-cadence`

## 2. 代码实现

- [x] 2.1 `app/ai.py`：新增 `_refresh_window_open(d: date) -> bool` helper
- [x] 2.2 `app/ai.py`：quarter 段加入窗口判定（仅窗口日检查 generated_week）
- [x] 2.3 `app/ai.py`：total 段窗口日才拉 README 比 sha（非窗口日对已有行不调用 get）
- [x] 2.4 `app/ai.py`：summary 段同 total 段窗口判定
- [x] 2.5 `app/ai.py`：同步模块头/recommend_missing/ensure_daily_ai docstring 与注释口径
- [x] 2.6 `.env.example`：`AI_README_HEAD_CHARS=3000` 并更新注释
- [x] 2.7 `app/schema.sql`：recommendations 表头注释同步半月窗口口径

## 3. 测试

- [x] 3.1 修改 `test_quarter_dimension_generated_and_weekly_republish`：改为窗口日重生/非窗口日跳过
- [x] 3.2 修改 `test_readme_sha_change_triggers_total_regeneration`：窗口日触发/非窗口日不触发
- [x] 3.3 修改 `test_summary_sha_change_regenerates`：窗口日触发/非窗口日不触发
- [x] 3.4 新增：窗口日 quarter/total/summary 重生正常触发
- [x] 3.5 新增：非窗口日既有 quarter/total/summary 行不重生且零 GitHub API 调用
- [x] 3.6 新增：非窗口日缺失行照常补缺生成
- [x] 3.7 新增/保留：周榜每周一不受窗口影响正常重生
- [x] 3.8 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1` 一次跑绿（407 passed）

## 4. 评审与交付

- [ ] 4.1 k3 独立评审，中/高 findings 清零（最多三轮）
- [ ] 4.2 A 级预演（真实/模拟环境验证窗口日/非窗口日行为）+ 复验步骤卡 → 本人走查
- [ ] 4.3 通过后归档 OpenSpec change（由主 agent 收口统一处理，本次不 commit）
