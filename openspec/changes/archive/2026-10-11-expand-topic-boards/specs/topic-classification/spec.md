# 规格 delta：主题分类（topic-classification）

## MODIFIED Requirements

### Requirement: 2026-10-11 封板词表口径
词表 SHALL 为 15 主题，顺序锁定：ai、frontend、backend、data、devops、devtools、security、game、automation、learning、mobile、embedded、media、selfhosted、blockchain（现有 9 主题顺序/label/词表不动，6 新主题追加尾部）。各主题词数锁定：ai 122、frontend 46、backend 54、data 36、devops 21、devtools 35、security 41、game 19、automation 18、learning 16、mobile 8、embedded 12、media 14、selfhosted 6、blockchain 14（总 462 词位、457 全局唯一词）。label 锁定：AI与智能、前端/UI、后端/云原生、数据库与数据工程、DevOps/监控、开发者工具、安全、游戏/图形、自动化/脚本、学习资源、移动、嵌入式/硬件、音视频/媒体、自托管/家居、区块链/金融。

#### Scenario: 词数锁定
- **WHEN** 加载 config/topics.yaml
- **THEN** 主题顺序与各主题词数等于上述锁定值（15 主题、总 462 词位），任一漂移即测试告警

#### Scenario: 清理后典型归属
- **WHEN** 以清理后词表判定：topics 含 `ssr` 的仓（如 nuxt）、含 `ci` 的仓（如 chinese-poetry）、含 `agent`/`agents` 的仓（如 dokploy）、含 `editor` 的仓（如 OpenCut）
- **THEN** 不再分别进入 security、devops、ai、devtools 主题榜（无其他命中时进"其他"榜）

#### Scenario: 扩展后典型归属
- **WHEN** 以扩展后词表判定：topics 含 `system-design`（如 system-design-primer）、含 `jellyfin`、含 `home-assistant`、含 `esp32`（如 zephyr）、含 `android`（如 termux 系）、含 `solidity`（如 viem/optimism 生态）
- **THEN** 分别进入 learning、media、selfhosted、embedded、mobile、blockchain 主题榜

## ADDED Requirements

### Requirement: 一词多主题双榜归属（mobile 与 frontend 共有词）
词表 SHALL 允许同一词条归属多主题（2026-10-11 本人拍板破例，词表首次打破"一词一主题"惯例）：`flutter`、`react-native`、`swiftui`、`jetpack-compose`、`kotlin-multiplatform` 同时收录于 frontend 与 mobile 词表，frontend MUST NOT 移除这些词。命中共有词的仓库 SHALL 按"命中多主题多榜重复"口径同时进 frontend 与 mobile 两榜。

#### Scenario: 共有词双榜命中
- **WHEN** 仓库 topics 含 `flutter`（frontend 与 mobile 共有词）
- **THEN** 同时命中 frontend 与 mobile 两主题（结果顺序按词表序，frontend 在前），该仓在两榜各出现一次

#### Scenario: 非共有词不跨榜
- **WHEN** 仓库 topics 含 `android`（仅收录于 mobile 词表）
- **THEN** 只命中 mobile 主题，不进 frontend 榜
