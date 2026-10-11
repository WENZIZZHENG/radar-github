# 规格 delta：语言榜（language-boards）

## MODIFIED Requirements

### Requirement: 语言榜集合与展示顺序
三口径（周/季/总星）分类榜 SHALL 共 28 张：语言组 12 张（11 语言榜＋"其它语言"）在前、主题组 16 张（词表 15 主题＋"其他"）在后。语言组内顺序 MUST 为 Java/Go/Rust/TypeScript/JavaScript/Python/C++/C/C#/Kotlin/Swift，"其它语言"恒在语言组末尾。边栏语言组项顺序 MUST 与榜块顺序一致；无 board 参数时默认首榜 MUST 为 language-java；`?board=` 合法值 MUST 包含 language-cpp、language-c、language-csharp、language-kotlin、language-swift，非法值静默降级为默认首榜。

#### Scenario: 榜齐备顺序固定
- **WHEN** 计算任一口径分类榜
- **THEN** 产出 28 张榜，前 12 张为语言组且第 7~11 张依次为 cpp/c/csharp/kotlin/swift、第 12 张为"其它语言"

#### Scenario: 新语言榜直达
- **WHEN** 用户访问 `/?board=language-kotlin`
- **THEN** 渲染 Kotlin 语言榜整页，边栏对应项 active
