# ai-refresh-cadence Specification

## Purpose
把 AI 文本的重生（REPLACE）节奏从「README 变更即重生 / 每周重生」收敛到每月 1 日与 15 日的半月窗口，并下调 README 入 prompt 的截断长度，以压降 AI 调用成本；缺失补缺不受窗口限制，周榜维度维持每周重生，总星文本与 AI 概要的 README 指纹口径不变。

## Requirements

### Requirement: 半月窗口判定

系统 SHALL 在 `app/ai.py` 中提供本地 helper `_refresh_window_open(d: date) -> bool`，当且仅当日期为每月 1 号或 15 号时返回 `True`。该判定只读 `now.date()`，不引入新依赖。判定按传入 `now` 的日期取值——调用方（`app/jobs.py`、手动同步入口）均传 UTC 时间，调度钉在 UTC 21:00（北京 05:00），因此窗口实际在北京时间每月 2 号、16 号的跑批触发；每月稳定两次、不漏窗口，属可接受的确定性偏移。

#### Scenario: 窗口日
- **WHEN** 跑批日期为某月 1 号或 15 号
- **THEN** `_refresh_window_open` 返回 `True`

#### Scenario: 非窗口日
- **WHEN** 跑批日期不是 1 号也不是 15 号
- **THEN** `_refresh_window_open` 返回 `False`

### Requirement: 季榜推荐语半月重生

系统 SHALL 对 `quarter` 维度推荐语执行半月重生：仅当跑批日期在窗口内，且既有行 `generated_week ≠ 当周` 时，才执行 `INSERT OR REPLACE` 重生；非窗口日对既有行完全跳过。**既有行 `source='manual'`（本地回填写入）时 MUST NOT 重生**：不拉 README、不比对、不调用 AI、不覆盖。缺失行（`cur is None`）任何日期都照常生成。

#### Scenario: 窗口日季榜过期
- **GIVEN** 仓库已有当季 quarter 行（`source='ai'`），且 `generated_week` 不是当周
- **WHEN** 跑批日期为窗口日
- **THEN** 调用 AI 生成新的 quarter 推荐语并 REPLACE 旧行，更新 `generated_week` 为当周

#### Scenario: 非窗口日季榜过期
- **GIVEN** 仓库已有当季 quarter 行，且 `generated_week` 不是当周
- **WHEN** 跑批日期不是窗口日
- **THEN** 不调用 AI、不拉 README、不更新 quarter 行

#### Scenario: 窗口日人工季榜行不重生
- **GIVEN** 仓库已有当季 quarter 行且 `source='manual'`、`generated_week` 不是当周
- **WHEN** 跑批日期为窗口日
- **THEN** 不拉 README、不比对、不调用 AI，该行文本、`generated_week`、`readme_sha` 全部保持不变

#### Scenario: 季榜缺失补缺
- **GIVEN** 仓库当季 quarter 行不存在
- **WHEN** 任意日期跑批
- **THEN** 照常生成 quarter 推荐语并写入（`source='ai'`）

### Requirement: 总榜推荐语半月重生

系统 SHALL 对 `total` 维度推荐语执行半月重生：仅当跑批日期在窗口内，且本次成功拉取到 README blob sha、且 sha 与库内旧指纹不同时，才执行 `INSERT OR REPLACE` 重生；非窗口日对既有行完全不拉 README、不比 sha、不重生。**既有行 `source='manual'` 时 MUST NOT 重生**（同季榜守卫）。缺失行任何日期都照常生成。

#### Scenario: 窗口日 README 变更
- **GIVEN** 仓库已有 total 行（`source='ai'`），库内指纹为 `sha-v1`
- **WHEN** 跑批日期为窗口日，且本次拉取到 `sha-v2`
- **THEN** 调用 AI 生成新的 total 推荐语并 REPLACE 旧行，更新 `readme_sha` 为 `sha-v2`

#### Scenario: 非窗口日 README 变更
- **GIVEN** 仓库已有 total 行，库内指纹为 `sha-v1`
- **WHEN** 跑批日期不是窗口日，且 README 实际已变为 `sha-v2`
- **THEN** 不拉 README、不比 sha、不调用 AI、不更新 total 行

#### Scenario: 窗口日 README 拉取失败
- **GIVEN** 仓库已有 total 行
- **WHEN** 跑批日期为窗口日，但 README 拉取失败（sha 未取到）
- **THEN** 不触发重生，保留旧行旧指纹（F2-1 守卫）

#### Scenario: 窗口日人工总榜行不重生
- **GIVEN** 仓库已有 total 行且 `source='manual'`，README 在窗口日前已变更
- **WHEN** 跑批日期为窗口日
- **THEN** 该行不被替换、指纹不被改写；该仓库的缺失维度（如 summary）仍照常补缺

#### Scenario: total 缺失补缺
- **GIVEN** 仓库 total 行不存在
- **WHEN** 任意日期跑批
- **THEN** 照常拉取 README 并生成 total 推荐语（`source='ai'`）

### Requirement: AI 概要半月重生

系统 SHALL 对 `summary` 维度执行与 `total` 维度同款的半月重生策略：仅窗口日对已有行拉 README 比 sha；非窗口日对已有行完全跳过；**既有行 `source='manual'` 时不重生**；缺失行任何日期都照常生成。

#### Scenario: 窗口日 README 变更
- **GIVEN** 仓库已有 summary 行（`source='ai'`），库内指纹为 `sha-v1`
- **WHEN** 跑批日期为窗口日，且本次拉取到 `sha-v2`
- **THEN** 调用 AI 生成新的 summary 概要并 REPLACE 旧行，更新 `readme_sha` 为 `sha-v2`

#### Scenario: 非窗口日 README 变更
- **GIVEN** 仓库已有 summary 行，库内指纹为 `sha-v1`
- **WHEN** 跑批日期不是窗口日
- **THEN** 不拉 README、不比 sha、不调用 AI、不更新 summary 行

#### Scenario: 窗口日人工概要行不重生
- **GIVEN** 仓库已有 summary 行且 `source='manual'`
- **WHEN** 跑批日期为窗口日且 README 已变更
- **THEN** 该行不被替换、指纹不被改写

### Requirement: 周榜维度不受半月窗口影响

系统 SHALL 保持 `week` 维度既有行为：每周一对新周标签生成新行，同周已存在行跳过；半月窗口判定不影响 week 段。

#### Scenario: 周一非窗口日
- **GIVEN** 周一为某月 2 号（非窗口日）
- **WHEN** 跑批发现新周标签缺失
- **THEN** 照常生成 week 推荐语

### Requirement: 手动批量补缺路径行为不变

系统 SHALL 保持 `recommend_missing(..., refresh=False)` 的核心行为：只补缺缺失行，对已有行不执行任何重生。实现差异留痕：改前 `refresh=False` 时 total 段对已有行仍会白拉一次 README 再跳过，改后在拉取前即跳过（落库结果与统计键不变，顺带省 GitHub API 调用，属良性变化）。

#### Scenario: refresh=False 非窗口日补缺
- **GIVEN** 手动批量 worker 以 `refresh=False` 调用
- **WHEN** 跑批日期不是窗口日
- **THEN** 缺失行照常生成，已有行完全跳过

### Requirement: README 截断上限配置

`.env.example` SHALL 将 `AI_README_HEAD_CHARS` 设置为 `3000` 作为降本后的现行口径；`app/config.py` 代码默认值保留不变，由 env 显式配置兜底。

#### Scenario: 新环境按模板配置
- **WHEN** 从 `.env.example` 复制出 `.env.dev`/`.env.prod`
- **THEN** `AI_README_HEAD_CHARS` 缺省为 `3000`
