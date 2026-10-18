# direct-follow Specification

## Purpose
让本人在已经知道一个 GitHub 仓库地址时，不依赖该项目先出现在榜单或搜索结果，就能把它直接加入「我的关注」：服务端把粘贴内容归一为 owner/repo，复用既有三态关注写路径落库，并用横幅如实反馈结果或失败原因。

## Requirements

### Requirement: 仓库地址解析与归一

系统 SHALL 在服务端把提交内容归一为 `owner/repo`，页面侧 MUST NOT 做任何地址解析。接受的形态：http/https 的 github.com 或 www.github.com 链接（可带子路径、查询串、片段、末尾斜杠、`.git` 后缀）、省略协议的 `github.com/owner/repo`、`git@github.com:owner/repo.git`、`ssh://git@github.com:owner/repo.git`，以及裸 `owner/repo`（仅在关注页接受；搜索页不接受裸名，避免 react/vue 这类词被误判）。归一结果 SHALL 大小写不敏感：命中库内记录时使用库内规范名，新入池与复活使用 GitHub 返回的规范名。

#### Scenario: 接受带子路径与查询串的链接

- **WHEN** 提交 `https://github.com/psf/requests/tree/main?tab=readme#intro`
- **THEN** 归一为 `psf/requests` 并进入关注流程

#### Scenario: 接受省略协议与 SSH 形态

- **WHEN** 依次提交 `github.com/psf/requests`、`github.com/psf/requests.git`、`git@github.com:psf/requests.git`、`ssh://git@github.com/psf/requests.git`、`psf/requests`
- **THEN** 五者均归一为 `psf/requests`

#### Scenario: 拒绝非 github.com 域名

- **WHEN** 提交 `https://gitlab.com/psf/requests`、`https://github.company.internal/psf/requests` 或 `https://notgithub.com/psf/requests`
- **THEN** 按无法识别处理并如实提示，不发起任何 GitHub 请求

#### Scenario: 拒绝只有 owner 或保留页前缀

- **WHEN** 提交 `psf`、`https://github.com/settings/profile` 或 `https://github.com/orgs/psf`
- **THEN** 按无法识别处理；GitHub 保留页前缀（settings/orgs/users/topics/features/marketplace 等站内入口）不被当作仓库名

#### Scenario: 拒绝非法字符与超长

- **WHEN** 提交含换行或空白分隔多个片段、裸形态多于两段路径（如 `psf/requests/extra`），或长度超过 owner 39＋repo 100 的输入
- **THEN** 按无法识别处理并如实提示，不发起任何 GitHub 请求

### Requirement: 关注页直接关注入口

「我的关注」页（P6）SHALL 在元信息行下、标签筛选行上提供地址输入框与「关注」按钮；关注列表为空时同样显示该入口。入口 MUST 在无 JavaScript 时可用（原生表单提交），提交语义与项目行右侧 ☆ 相同，MUST NOT 调用 AI。成功后该仓库行 SHALL 出现在对应语言分组（无增量数据时沿用"—— 下周起有数据"标注），顶栏关注计数与实际关注数一致。

#### Scenario: 空关注页仍显示入口

- **GIVEN** 关注数为 0，页面处于空关注态
- **WHEN** 打开「我的关注」页
- **THEN** 地址输入框与「关注」按钮可见，空态说明仍在

#### Scenario: 提交新地址后新行落组

- **GIVEN** 某仓库不在关注列表且未入池
- **WHEN** 在关注页粘贴其地址并提交
- **THEN** 页面回到「我的关注」，结果横幅显示，该仓库行出现在其语言分组，无增量数据时标注"—— 下周起有数据"

#### Scenario: 重复关注幂等

- **GIVEN** 某仓库已在关注列表
- **WHEN** 再次提交该仓库地址
- **THEN** 结果横幅为"已经在关注中"，关注列表不出现重复行，库内零变化

#### Scenario: 无 JavaScript 可用

- **GIVEN** 浏览器禁用 JavaScript
- **WHEN** 在关注页提交地址
- **THEN** 表单原生提交完成关注并整页渲染结果，功能不降级

### Requirement: 结果与失败如实反馈

成功 SHALL 走 PRG：服务端 303 跳回 `/follows?added=<规范名>&result=<joined|revived|alive|already>`，由随后的 GET 渲染结果横幅，横幅文案固定为四种之一（见场景）。横幅 SHALL 附"在下方「{语言组}」组查看"的锚点链接（锚点形如 `#f-python`；无语言归「其它语言」组）。失败 MUST NOT 跳转：在同一页面渲染原因并保留输入框原值，HTTP 状态与错误类别一致——400 入参无法识别、404 仓库不存在、409 node_id 撞库（改名或同名重建）、500 GitHub token 配置错误、502 GitHub 请求失败。

#### Scenario: 新入池（joined）

- **WHEN** 提交一个此前不在池内的仓库地址并关注成功
- **THEN** 303 跳回 `/follows?added=<规范名>&result=joined`，横幅为 `已关注 {full_name}：此前不在池内，已即时抓取基线快照并入池，下周起显示当周增量。`

#### Scenario: 复活（revived）

- **WHEN** 提交一个库内已标记失效（dead）的仓库地址并关注成功
- **THEN** 303 跳回 `/follows?added=<规范名>&result=revived`，横幅为 `已关注 {full_name}：此前已标记失效，已复活并补一张基线快照，次日起恢复每日采集。`

#### Scenario: 已入池（alive）

- **WHEN** 提交一个库内 alive 且未关注的仓库地址并关注成功
- **THEN** 303 跳回 `/follows?added=<规范名>&result=alive`，横幅为 `已关注 {full_name}：该仓库已在跟踪池内，无需额外抓取。`

#### Scenario: 已关注（already）

- **WHEN** 提交一个已在关注列表的仓库地址
- **THEN** 303 跳回 `/follows?added=<规范名>&result=already`，横幅为 `{full_name} 已经在关注中，未做重复写入。`

#### Scenario: 横幅带语言组锚点

- **WHEN** 成功横幅渲染时该仓库在库内有语言归属
- **THEN** 横幅附"在下方「{语言组}」组查看"链接，指向 `#f-<语言 key>`；无语言时指向「其它语言」组的锚点

#### Scenario: 输入无法识别（400）

- **WHEN** 提交的内容无法归一为 owner/repo
- **THEN** HTTP 400，同页渲染 `无法识别仓库地址：{输入}。支持 https://github.com/owner/repo、git@github.com:owner/repo.git 或 owner/repo。`，输入框保留原值

#### Scenario: 仓库不存在（404）

- **WHEN** 归一成功但 GitHub 上不存在该仓库（不存在/已删除/转私有）
- **THEN** HTTP 404，同页渲染 `GitHub 上找不到仓库 {full_name}（不存在/已删除/转私有）`，输入框保留原值

#### Scenario: 改名或同名重建（409）

- **WHEN** 归一成功但该仓库的 node_id 与库内既有记录冲突（仓库已改名或同名重建）
- **THEN** HTTP 409，沿用既有 node_id 冲突文案（仓库可能已改名/同名重建，暂未自动归并），输入框保留原值

#### Scenario: token 与上游失败（500/502）

- **WHEN** GitHub token 未配置/无效，或 GitHub 请求失败
- **THEN** HTTP 500（沿用既有 GitHub token 配置错误文案）或 HTTP 502（`GitHub 请求失败：{原因}`），均同页渲染并保留输入框原值

### Requirement: 关注写路径唯一

系统 SHALL 只经既有三态关注写路径落库：已入池 alive 只补 follows 行；已入池 dead 复活并补一张基线快照；未入池抓单仓元数据入池（`source='follow'`）并补基线快照。MUST NOT 新增第二条关注写路径，MUST NOT 新增 JSON 接口。已入池仓库 MUST 为 0 次 GitHub 调用，未入池与复活各 MUST 为 1 次；重复关注 MUST 幂等短路（不重复写入、不调用 GitHub），取消关注只删 follows 行、不动仓库与快照。

#### Scenario: 已入池地址不调用 GitHub

- **GIVEN** 仓库在库内 alive 且未关注
- **WHEN** 提交其地址
- **THEN** 库内只新增 follows 行，仓库记录与快照零变化，GitHub 调用 0 次

#### Scenario: 未入池地址一次调用完成入池

- **WHEN** 提交一个库内不存在的仓库地址
- **THEN** 恰好 1 次 GitHub 单仓请求，写入 `repos(source='follow')`、一张基线快照与 follows 行

#### Scenario: 失效仓库一次调用完成复活

- **WHEN** 提交一个库内 dead 的仓库地址
- **THEN** 恰好 1 次 GitHub 单仓请求，该仓库复活（不再标记失效）、补一张基线快照并写入 follows 行

#### Scenario: 大小写不同命中库内记录

- **WHEN** 提交 `PSF/Requests` 而库内记录为 `psf/requests`
- **THEN** 命中库内记录，GitHub 调用 0 次，横幅与跳转参数使用库内规范名

#### Scenario: 已关注幂等短路

- **WHEN** 提交一个已在关注列表的仓库地址
- **THEN** 不写库、不调用 GitHub，返回是否已关注的幂等结果

### Requirement: 防御性提交边界

正常表单之外的异常直连提交 SHALL 被防御性拒绝：请求体超过 8192 字节时返回 400 与固定短文案且不回显原文（带 Content-Length 时在读取请求体前拒绝）；表单 POST 的 Origin 与站点不同源时返回 403、不进入关注写路径。这两类防御分支不改变正常路径「失败同页渲染并保留输入原值」的口径。

#### Scenario: 请求体超限

- **WHEN** 直连提交的请求体超过 8192 字节（正常表单 maxlength=300 不可达）
- **THEN** HTTP 400，渲染固定短文案「输入过长」，不回显提交原文、不写库、不调用 GitHub

#### Scenario: 跨站表单提交

- **WHEN** 表单 POST 携带的 Origin 与请求 Host / X-Forwarded-Host 均不同源
- **THEN** HTTP 403（防御分支返回 JSON 提示，不渲染 P6），不进入关注写路径、不写库、不调用 GitHub

### Requirement: 搜索页引导不改变检索行为

智能搜索页（P8）SHALL 在搜索框下提供一行静态引导，指向「我的关注」页添加已知仓库地址。该引导 MUST NOT 改变搜索表单的输入契约（自然语言描述）、检索链路与流式加载态的事件协议；把仓库地址提交到搜索框仍按既有自然语言链路处理，系统 MUST NOT 在搜索链路中新增地址识别或直接关注分支。AI 不可用时搜索页的既有降级形态（表单禁用＋提示）不变。

#### Scenario: 引导可见且表单契约不变

- **WHEN** 打开搜索页
- **THEN** 搜索框下有一行指向「我的关注」页的引导提示；搜索框仍只接受自然语言描述，表单字段与提交目标不变

#### Scenario: 地址粘到搜索框仍走自然语言

- **WHEN** 把 GitHub 仓库地址提交到搜索框
- **THEN** 仍按既有自然语言检索链路处理，不出现直接关注结果

#### Scenario: AI 不可用时引导仍在

- **GIVEN** DeepSeek 未配置或已禁用，搜索页表单禁用
- **WHEN** 打开搜索页
- **THEN** 引导提示照常显示，既有降级提示与禁用形态不变

### Requirement: 既有边界不扩大

本能力 MUST NOT 扩大关注边界：不自动给改名仓库归并——库内 `full_name` 不改写、node_id 冲突按 409 如实报告；单仓元数据请求跟随 GitHub 的 301 跳转，旧链接指向未入池仓库时按新名入池，已跟踪仓库则 409；不支持批量多地址、非 GitHub 域名与添加时打标签。

#### Scenario: 旧链接指向未入池仓库

- **GIVEN** 某仓库已改名，旧地址仍会被 GitHub 301 到新名，新名不在库内
- **WHEN** 提交旧地址
- **THEN** 跟随跳转后按新名入池，横幅使用新名

#### Scenario: 旧链接指向已跟踪仓库

- **GIVEN** 旧名在库内（未关注），新名对应的 node_id 已存在于另一条记录
- **WHEN** 提交旧地址
- **THEN** HTTP 409 如实报告，库内 `full_name` 不被改写、不自动归并

#### Scenario: 多地址不做批量

- **WHEN** 提交多行或多地址拼接的输入
- **THEN** 按单地址解析失败处理（400），不批量关注其中任何一个

#### Scenario: 添加时不打标签

- **WHEN** 在地址后附加标签语法（如 `owner/repo #标签`）
- **THEN** 按地址无法识别处理，不写入标签
