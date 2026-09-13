# 规格变更：ai-refresh-cadence（local-ai-relay 引入人工行来源概念）

## MODIFIED Requirements

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
