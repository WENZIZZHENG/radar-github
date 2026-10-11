# language-boards Specification

## Purpose
定义语言分类与语言榜集合的行为契约：哪些 GitHub 主语言独立成榜、榜单 key 与展示顺序、未列出语言与无语言仓库的归集口径，以及该语言集合向榜单页、关注页分组、智能搜索的派生一致性。

## Requirements

### Requirement: 语言精确分类与榜单 key 映射
系统 SHALL 按 GitHub `primaryLanguage.name` 精确值（首字母大写，不做 lower 归一）将仓库分入 11 个语言榜，映射为：Java→java、Go→go、Rust→rust、TypeScript→typescript、JavaScript→javascript、Python→python、C++→cpp、C→c、C#→csharp、Kotlin→kotlin、Swift→swift。榜单 key MUST 全小写且无符号（URL/查询参数安全）。大小写变体或非法语言名 MUST NOT 归一入榜（宁可落"其它语言"不误进指定榜）。

#### Scenario: 11 个指定语言各自成榜
- **WHEN** 仓库主语言为 "C++"、"C"、"C#"、"Kotlin" 或 "Swift"
- **THEN** 分别归入 cpp、c、csharp、kotlin、swift 语言榜，不再进"其它语言"榜

#### Scenario: 大小写变体不归一
- **WHEN** 语言字段出现 "cpp"、"JAVA" 等非 GitHub 精确名的变体
- **THEN** 一律归"其它语言"榜

### Requirement: 语言榜集合与展示顺序
三口径（周/季/总星）分类榜 SHALL 共 28 张：语言组 12 张（11 语言榜＋"其它语言"）在前、主题组 16 张（词表 15 主题＋"其他"）在后。语言组内顺序 MUST 为 Java/Go/Rust/TypeScript/JavaScript/Python/C++/C/C#/Kotlin/Swift，"其它语言"恒在语言组末尾。边栏语言组项顺序 MUST 与榜块顺序一致；无 board 参数时默认首榜 MUST 为 language-java；`?board=` 合法值 MUST 包含 language-cpp、language-c、language-csharp、language-kotlin、language-swift，非法值静默降级为默认首榜。

#### Scenario: 榜齐备顺序固定
- **WHEN** 计算任一口径分类榜
- **THEN** 产出 28 张榜，前 12 张为语言组且第 7~11 张依次为 cpp/c/csharp/kotlin/swift、第 12 张为"其它语言"

#### Scenario: 新语言榜直达
- **WHEN** 用户访问 `/?board=language-kotlin`
- **THEN** 渲染 Kotlin 语言榜整页，边栏对应项 active

### Requirement: 未列出语言与无语言归"其它语言"
主语言不在 11 个指定语言内、或语言为 null 的仓库 SHALL 归"其它语言"榜。Shell、HTML、Jupyter Notebook、PHP MUST 保持归"其它语言"（不独立成榜）。

#### Scenario: 无语言仓库
- **WHEN** 仓库 language 为 null
- **THEN** 归入"其它语言"榜，不进任何指定语言榜

#### Scenario: 未列出语言仓库
- **WHEN** 仓库主语言为 "PHP" 或 "HTML"
- **THEN** 归入"其它语言"榜

### Requirement: 语言集合派生一致性
语言集合 MUST 单一事实源于分类器的语言映射表，以下派生面 MUST 跟随同一集合、不得各自硬编码：榜单计算与边栏白名单、P6 关注页语言分组（组头展示名为 GitHub 精确名，"其它语言"组为中文名）、P8 智能搜索意图理解的 languages 合法取值（大小写不敏感归一到 GitHub 精确名）。

#### Scenario: 关注页分组跟随
- **WHEN** 用户关注了一个主语言为 "Kotlin" 的仓库
- **THEN** 该仓在关注页出现在"Kotlin"语言组，不再出现在"其它语言"组

#### Scenario: 智能搜索语言归一
- **WHEN** 搜索意图理解产出 languages 含 "c++" 或 "C#"
- **THEN** 归一为 "C++"/"C#" 参与过滤；仍非法的语言名（如 "Cobol"）丢弃
