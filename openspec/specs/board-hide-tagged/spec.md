# 规格：榜单"隐藏已打标"过滤（board-hide-tagged）

## Purpose

提供榜单"隐藏已打标"过滤（P10）：hide_tagged=1 时主榜/新崛起区/新项目区不显示已打标行＋计数提示，默认关闭＝完整榜单口径基线。本规格由 change `board-hide-tagged` 归档沉淀（2026-08-23）。

## Requirements

### Requirement: hide_tagged URL 参数契约
榜单四页（P1 本周报告 `/`、P2 历史周报 `/?week=`、P3 季度回顾 `/quarter`、P4 总星榜 `/total`，含单榜整页 `?board=`）SHALL 接受查询参数 `hide_tagged`。仅 `hide_tagged=1` 视为开启；缺省、空值、其他取值 MUST 一律按关闭处理（静默降级，不报 400）。缺省关闭时页面行为与本变更前完全一致（完整榜单是口径基线）。

#### Scenario: 缺省访问渲染完整榜单
- **WHEN** 不带 `hide_tagged` 参数访问任一榜单页
- **THEN** 主榜、新项目区、新崛起区渲染全部行（含已打标仓），与本变更前一致

#### Scenario: 非法值静默按关闭
- **WHEN** 以 `hide_tagged=0` 或 `hide_tagged=yes` 访问
- **THEN** 页面按关闭渲染，不报错

### Requirement: 服务端过滤范围
开启时，系统 SHALL 在展示装配层对本次渲染的所有榜统一过滤：任一仓在该仓 tags 表存在任意标签（"已打标"）时，其在主榜、新项目区、新崛起区的行 MUST NOT 渲染。过滤 MUST NOT 改变榜单计算口径（出席/排序/Top N/三区互斥）与 board_cache 结构；首期空态降级判定 MUST 跑在过滤前的 boards 上（隐藏不触发也不抑制降级；降级渲染的总星榜行同样应用过滤）。dead 仓有标签时同样隐藏。

#### Scenario: 已打标行三区消失
- **WHEN** `hide_tagged=1` 且仓 X 打过标签，X 本在主榜第 3 行、新项目区或新崛起区也可能有其身影
- **THEN** X 的所有行不渲染；榜单其余行顺序与数量口径不变（不补位）

#### Scenario: 隐藏不影响首期降级判定
- **WHEN** 增量榜三区全空触发首期降级为总星榜，且 `hide_tagged=1`
- **THEN** 降级照常发生，渲染的总星榜行应用同样的已打标过滤

### Requirement: 隐藏计数提示
开启且本次渲染实际隐藏行数 N>0 时，页面 SHALL 渲染一行"已隐藏 N 个已打标项目"小字提示（位于元信息行与榜单区之间），并附回到关态的链接；N MUST 按被隐藏的行数计（同仓多榜重复出现按行分别计）；N=0 时 MUST NOT 渲染该提示行。

#### Scenario: 计数按行
- **WHEN** 开启后本周页共隐藏 7 行（其中同仓在两榜各出现一次）
- **THEN** 提示行显示"已隐藏 7 个已打标项目"

#### Scenario: 无已打标行不出提示
- **WHEN** 开启但本页没有任何行被隐藏
- **THEN** 不渲染计数提示行（开关本身仍显示）

### Requirement: 榜块空态与计数口径
开启后某榜三区行全部被隐藏时，该榜块 SHALL 渲染榜头＋一行"已全部隐藏"空态小字，MUST NOT 渲染区头与空壳。榜头数量、区头徽标、边栏徽标 MUST 保持过滤前的完整榜单口径，不随隐藏缩水。

#### Scenario: 单榜全隐藏
- **WHEN** `?board=language-rust&hide_tagged=1` 且该榜在渲染集内的行全部已打标
- **THEN** 该榜块显示榜头与"已全部隐藏"空态小字；边栏徽标仍显示完整口径数量

### Requirement: 开关状态随 URL 透传
开关 SHALL 为页内 GET 链接（开/关互跳，链接保留当前 `week=`/`quarter=`/`board=` 参数）。开启时，边栏项链接、期次分段控件链接 MUST 携带 `hide_tagged=1`；关闭态链接 MUST NOT 携带该参数。P5 标签页、P6 关注页不涉及本开关。

#### Scenario: 跨期次导航不丢开关
- **WHEN** 在历史周页 `/?week=2026-W33&hide_tagged=1` 点边栏某主题榜
- **THEN** 跳转到 `/?board=<该榜>&week=2026-W33&hide_tagged=1`，开关保持开启

#### Scenario: 关态链接干净
- **WHEN** 开关关闭时点"隐藏已打标"
- **THEN** 跳转到当前页 URL 追加 `hide_tagged=1`；再点"显示全部"回到无该参数的 URL
