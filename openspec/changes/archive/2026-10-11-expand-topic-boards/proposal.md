# 提案：主题榜扩容（expand-topic-boards）

## Why

生产快照实测（2026-10-11 主 agent 探库）：活仓 10,681 中 4,836 仓（45%）主题零命中落"其他"榜——其中 2,397 仓无任何 topics（词表救不了），2,439 仓有 topics 但未命中现有 392 词词表（扩展空间）。现有 9 主题命中分布：ai 1928 / frontend 1559 / backend 1295 / devtools 1247 / security 740 / data 725 / automation 441 / devops 400 / game 349。"其他"榜占近半稀释主题榜信号；学习资源、移动、嵌入式、音视频、自托管、区块链六个方向在零命中仓中经逐词实测命中量＋抽样纯度验证，体量与纯度均足以独立成榜。2026-10-11 本人拍板：主题 9 → 15。

## What Changes

- **只改词表数据，不改匹配逻辑**：`config/topics.yaml` 追加 6 个主题（learning / mobile / embedded / media / selfhosted / blockchain），插现有 9 主题之后（automation 尾部），排列 learning→mobile→embedded→media→selfhosted→blockchain；词位 392 → 462，全局唯一词 392 → 457（mobile 有 5 词与 frontend 共有，见下）。`app/classify.py` 的 `classify_topics` 匹配逻辑零改动；现有 9 主题的顺序/label/词表一律不动。
- **双榜破例（一词多主题首次，2026-10-11 本人拍板）**：mobile 的 `flutter`/`react-native`/`swiftui`/`jetpack-compose`/`kotlin-multiplatform` 与 frontend 词表共有——留双榜，frontend 不搬家，标这些词的仓同时进 frontend 和 mobile 两榜（词表首次打破"一词一主题"惯例，理由见 design.md 决策 3）。
- **逐词 diff 表**（封板依据，经本人过目封板后才动 topics.yaml）：

  **learning / 学习资源（16 词）**——典型仓：donnemartin/system-design-primer、CyC2018-CS-Notes

  | 词条 | 动作 | 收词理由 |
  | --- | --- | --- |
  | `awesome` | 新增 | 学习资源榜主力词，awesome 系列清单仓 |
  | `awesome-list` | 新增 | awesome 系列显式变体 |
  | `computer-science` | 新增 | CS 学习资源核心词 |
  | `algorithm` | 新增 | 算法学习/实现仓（尾"s"归一已覆盖 topics `algorithms`，不收复数词条） |
  | `interview` | 新增 | 面试资源 |
  | `interview-questions` | 新增 | 面试题库 |
  | `interview-preparation` | 新增 | 面试准备 |
  | `leetcode` | 新增 | 刷题仓 |
  | `data-structures` | 新增 | 数据结构 |
  | `book` | 新增 | 书籍资源 |
  | `education` | 新增 | 教育仓 |
  | `tutorial` | 新增 | 教程 |
  | `learning` | 新增 | 学习资源语义核心词 |
  | `system-design` | 新增 | 系统设计学习资源（system-design-primer 类） |
  | `cheatsheet` | 新增 | 速查表 |
  | `course` | 新增 | 课程 |

  **mobile / 移动（8 词）**——典型仓：termux 系

  | 词条 | 动作 | 收词理由 |
  | --- | --- | --- |
  | `android` | 新增 | Android 平台主力词 |
  | `ios` | 新增 | iOS 平台主力词 |
  | `mobile` | 新增 | 移动语义核心词 |
  | `flutter` | 新增 | 跨端框架（与 frontend 共有，双榜破例） |
  | `react-native` | 新增 | 同上 |
  | `swiftui` | 新增 | 同上 |
  | `jetpack-compose` | 新增 | 同上 |
  | `kotlin-multiplatform` | 新增 | 同上 |

  **embedded / 嵌入式/硬件（12 词）**——典型仓：zephyr

  | 词条 | 动作 | 收词理由 |
  | --- | --- | --- |
  | `embedded` | 新增 | 嵌入式核心词 |
  | `esp32` | 新增 | ESP32 硬件生态 |
  | `esp8266` | 新增 | ESP8266 硬件生态 |
  | `arduino` | 新增 | Arduino 生态 |
  | `firmware` | 新增 | 固件 |
  | `iot` | 新增 | 物联网 |
  | `raspberry-pi` | 新增 | 树莓派 |
  | `robotics` | 新增 | 机器人 |
  | `risc-v` | 新增 | RISC-V 架构 |
  | `fpga` | 新增 | FPGA |
  | `microcontroller` | 新增 | 单片机 |
  | `stm32` | 新增 | STM32 生态 |

  **media / 音视频/媒体（14 词）**——典型仓：jellyfin

  | 词条 | 动作 | 收词理由 |
  | --- | --- | --- |
  | `music` | 新增 | 音乐主力词 |
  | `audio` | 新增 | 音频主力词 |
  | `video` | 新增 | 视频主力词 |
  | `ffmpeg` | 新增 | FFmpeg 生态 |
  | `jellyfin` | 新增 | Jellyfin 媒体服务器生态 |
  | `music-player` | 新增 | 音乐播放器 |
  | `iptv` | 新增 | IPTV |
  | `spotify` | 新增 | Spotify 生态 |
  | `mpv` | 新增 | mpv 播放器生态 |
  | `podcast` | 新增 | 播客 |
  | `video-player` | 新增 | 视频播放器 |
  | `audio-player` | 新增 | 音频播放器 |
  | `media-server` | 新增 | 媒体服务器 |
  | `midi` | 新增 | MIDI |

  **selfhosted / 自托管/家居（6 词）**——典型仓：home-assistant 生态

  | 词条 | 动作 | 收词理由 |
  | --- | --- | --- |
  | `self-hosted` | 新增 | 自托管核心词（连字符形态） |
  | `selfhosted` | 新增 | 同上（连写变体） |
  | `home-assistant` | 新增 | Home Assistant 生态（连字符形态） |
  | `homeassistant` | 新增 | 同上（连写变体） |
  | `plex` | 新增 | Plex 生态 |
  | `nextcloud` | 新增 | Nextcloud 生态 |

  **blockchain / 区块链/金融（14 词）**——典型仓：viem、optimism

  | 词条 | 动作 | 收词理由 |
  | --- | --- | --- |
  | `blockchain` | 新增 | 区块链核心词 |
  | `ethereum` | 新增 | 以太坊生态 |
  | `bitcoin` | 新增 | 比特币生态 |
  | `cryptocurrency` | 新增 | 加密货币 |
  | `cryptocurrencies` | 新增 | 加密货币复数变体（y→ies 不受尾"s"归一覆盖，实测 5 仓） |
  | `solidity` | 新增 | Solidity 合约开发 |
  | `web3` | 新增 | Web3 |
  | `smart-contract` | 新增 | 智能合约 |
  | `wallet` | 新增 | 钱包 |
  | `solana` | 新增 | Solana 生态 |
  | `finance` | 新增 | 金融 |
  | `quant` | 新增 | 量化 |
  | `trading` | 新增 | 交易 |
  | `algorithmic-trading` | 新增 | 程序化交易 |

- **榜单集合用户可见口径变更**：三口径分类榜 22 张 → 28 张（语言组 12 张不变；主题榜 10 → 16：15 主题＋"其他"）；边栏主题组 10 项 → 16 项；`?board=` 合法值新增 `topic-learning`/`topic-mobile`/`topic-embedded`/`topic-media`/`topic-selfhosted`/`topic-blockchain`，默认首榜仍为 `language-java`；主题榜无边栏点色（LANG_COLORS 只服务语言榜），新主题无需配色。
- **派生面自动跟随**（均从词表动态派生、无第二数据源）：榜单装配（compute_boards 遍历词表）、边栏白名单（`_board_key_whitelist` 动态生成）、边栏主题组——零逻辑改动。
- **封板物同步**：`config/topics.yaml` 头注释补"2026-10-11 扩展封板（+6 主题 70 词位，392→462 词位/457 唯一词）"；`docs/sop/架构决策记录.md` 附录 yaml 块一字同步＋收词沿革补一条扩展记录（关联本 change）；`tests/test_classify.py` 词数锁定断言更新为新分布。
- **生效链路**：改词表后重启服务（页面层 `_topic_table()` 模块级缓存）＋手动跑一次预计算刷新 board_cache（词表不在缓存失效判定内）；不手动跑则次日 05:00 每日任务自动以新词表重算。主题榜成员变化属用户可见口径变更。
- **路径唯一性自查（裁定 9）**：本 change 不涉及入口/注册/登录/权限/删除/数据归属；主题分类维持唯一路径——`classify_topics` 纯函数 × `config/topics.yaml` 封闭词表，无第二份词表、无第二条分类路径（逐条自查见 design.md"路径唯一性自查"）。
- **不动的部分**：现有 9 主题顺序/label/词表；匹配规则（精确＋尾 s 归一）；"其他"榜口径；语言榜 12 张；采集、打标、翻译/推荐链路。《示意图.html》《原型-T021.html》为视觉冻结物本次不动，沿用已知漂移登记口径（design.md 留痕）。

## Capabilities

### New Capabilities

（无——主题分类 capability 由在途 change `topic-wordlist-cleanup` 引入，本 change 在其口径上扩容，不另立新 capability。）

### Modified Capabilities

- `topic-classification`: 封板词表口径锁定值变化——主题 9 → 15（顺序锁定追加 learning/mobile/embedded/media/selfhosted/blockchain）、词数锁 392 → 462 词位（457 全局唯一词）、label 锁增 6 个；新增"一词多主题双榜归属"要求（mobile 与 frontend 共有 5 词，词表首次打破一词一主题）。注：`topic-classification` 由在途 change `topic-wordlist-cleanup` 以 ADDED 引入、尚未归档沉淀进 `openspec/specs/`；本 change 的实施与归档均排在其后（词数锁基线即其 392 分布）。
- `language-boards`: expand-language-boards 引入的"语言榜集合与展示顺序"要求中"三口径分类榜共 22 张/主题组 10 张"随本 change 更新为"共 28 张/主题组 16 张"（主题榜 10→16 是本 change 的直接后果；2026-10-11 主 agent 拍板，design.md 决策 10）。注：`language-boards` 由在途 change `expand-language-boards` 以 ADDED 引入、尚未归档；归档顺序钉死 topic-wordlist-cleanup → expand-language-boards → expand-topic-boards。

## Impact

- **代码**：无逻辑改动。`config/topics.yaml` 词表数据（追加 6 主题 70 词位＋头注释补封板记录）；`app/report.py:343,346`、`app/web/routes.py:642,646,732,747,946`、`app/schema.sql:94`、`app/web/templates/boards.html:60` 仅注释中"22 榜/9 主题"等数量表述刷新（榜单装配、白名单、边栏全部从词表动态派生）。
- **测试**：`tests/test_classify.py`——词数锁定断言更新为新分布（ai 122 / frontend 46 / backend 54 / data 36 / devops 21 / devtools 35 / security 41 / game 19 / automation 18 / learning 16 / mobile 8 / embedded 12 / media 14 / selfhosted 6 / blockchain 14）；主题顺序锁 9 → 15、label 锁增 6；新增每主题至少一条精确命中用例＋一条单复数归一用例；新增一条双榜断言（topics=["flutter"] 同时命中 frontend 与 mobile）。`tests/test_report.py`、`tests/test_board_cache.py`、`tests/test_web.py` 的榜单计数常量 22/23 → 28/29 参照 expand-language-boards 同口径更新。
- **文档**：`README.md` 数量表述 22 → 28、"9 主题"→"15 主题"；产品层冻结物（《需求清单》《功能闭环清单》《共识文档》《交互流程说明》《交互流程/12、13》）数量表述全量回写（验收通过后按 §1 口径执行，列进 tasks）。
- **数据**：无 schema 变更、无迁移；board_cache 次日预计算自然刷新，或部署后手动跑一次立即生效。
- **用户可见口径**：主题榜 10 → 16 张、总榜 22 → 28 张；6 张新主题榜成形，"其他"榜瘦身（幅度以 A 级预演实测为准）；命中共有词的仓同时出现在 frontend 与 mobile 两榜。
- **外部依赖**：无新增。
- **项目 SOP**：改用户可见榜单口径＋触核心链路（榜单计算 → 展示）→ OpenSpec change 先行（本 change）；零页面/交互/视觉元素变化（无边栏点色新增），免示意图与《交互流程说明》流程更新（仅数量表述回写）；A 级预演（真实库手动预计算＋榜单成形核对＋典型仓抽查）；独立评审子 agent（核心链路口径变更）。
