# 设计：直接关注（direct-follow）

## Context

- 关注＝动态入池（《架构决策记录》决策 9）。唯一写路径是 `app/follows.py:50` 的 `follow_repo`，按库内状态分三态：已入池 alive 只补 follows 行；已入池 dead 复活（`dead=0`）＋抓一张基线快照；未入池抓单仓元数据入池（`source='follow'`）＋基线快照。重复关注在 `app/follows.py:65` 幂等短路；取消关注只删 follows 行（`unfollow_repo`，跟踪保留）。
- `POST /api/follows`（`app/web/routes.py:1201`）已接受 `owner/repo` 并映射错误：400 入参非法（`_parse_full_name`，`app/web/routes.py:1149`，形态与长度判定在 `app/reporef.py:39-40`）／404 仓库不存在／409 node_id 撞库／500 GitHub token／502 GitHub 请求失败。这些异常类型来自 `app/collector/github.py` 与 `app/collector/snapshot.py` 的 fail-loud `ValueError`。
- 唯一关注入口是项目行右侧 ☆（`app/web/templates/_row.html:18`），前提是该仓库已出现在榜单或搜索结果里；「我的关注」页（`app/web/templates/follows.html`）无添加入口，空态文案只指向榜单 ☆。P6 按语言分 12 组（`_follow_groups`，`app/web/routes.py:379`），锚点形如 `f-python`，无语言归 `other`（「其它语言」）。
- P8 智能搜索是 AI 自然语言链路（`app/search.py:430` `run_search`）；AI 不可用时整页禁用表单（`app/web/templates/search.html:12`）。
- 仓库改名后库内 `full_name` 不更新（每日快照只更新简介/语言/topics/创建时间）；新名字撞 node_id → `ingest_followed_repo` 抛 `ValueError` → 409，既有口径是 fail-loud、不自动归并。
- `follow_repo` 当前的库内查找是大小写敏感等值查询（`WHERE full_name = ?`）；`repos.full_name` 是 BINARY 唯一索引。单仓 GET（`GitHubClient.fetch_repo`，`app/collector/github.py:205`）走共享的 `_request`，httpx 客户端未开跟随重定向（默认关闭），改名后 GitHub 返回的 301 会被当作普通响应返回。
- 本地库现状：alive 仓 10,681 个、关注 1 条；统一验证入口 `tools/verify.ps1`（ruff + pytest），基线 586 passed。

## Goals / Non-Goals

**Goals:**

- 地址解析只有一个服务端入口，纯函数、可穷举单测；接受/拒绝矩阵与 spec 的 Requirement 一一对应，前端零解析。
- 关注页入口与结果反馈在 SSR 内闭环：无 JS 可用；成功走 PRG；失败同页渲染并保留输入。
- 写路径零新增：`POST /follows/add` 只做「解析 → 调既有三态写路径 → 映射反馈」三件事。
- 服务端改动面可回归：`follow_repo` 只动「查找口径」与「回传规范名」，`fetch_repo` 只动「跟随重定向」，其余文件是新增入口与模板/样式。

**Non-Goals:**

- 不改 schema、不做数据迁移、不新增依赖与环境变量。
- 不改采集节奏、榜单计算、AI 链路与搜索检索链路。
- 不做前端地址预校验（不在 JS 里判断地址合法性）。
- 不做批量、不做非 GitHub 域名、不做改名自动归并、不做添加时打标签。
- 不为失败分支新开错误页模板，也不为 P6 另造一套页面上下文。

## Decisions

### 1. 解析收口在服务端单一模块（`app/reporef.py`），纯函数、无 IO

- 接口形态：两个纯函数——`parse_github_ref(raw)` 只认带域名的形态（URL / SSH），`parse_repo_ref(raw)` 在其上再接受裸 `owner/repo`（关注页用后者）。失败一律返回 `None`；用户可见文案统一为 spec 那一句 400 文案（不按原因分叉，避免文案漂流），失败原因只进日志与测试断言，不扩大公开返回形态。
- 处理顺序：去首尾空白 → 识别 `git@host:path`、`ssh://`、有/无 scheme 三种形态 → 取 host 并要求精确等于 `github.com` 或 `www.github.com`（子域名与其他站点一律拒绝，含企业版）→ 取 path 前两段作为 owner/repo，去掉 `.git` 后缀（带域名/协议前缀的链接允许子路径；裸形态要求恰好两段，允许末尾斜杠与 `.git` 后缀，多于两段按拒绝）→ 校验 owner/repo 字符集与长度（沿用 `_FULL_NAME_RE` 与 owner 39＋repo 100 的同口径，上限 140）→ **owner 段含 `.` 一律拒绝**（GitHub 用户名/组织名不含点，借此挡住省略协议的域名两段形态如 `gitlab.com/psf`）→ 校验 owner 不在 GitHub 保留页前缀清单里。
- 裸名兜底守卫：带域名/协议特征（含 `://`、`git@`，或以 `github.com/`、`www.github.com/` 开头）但域名形态解析失败的输入（如 `github.com/owner` 缺 repo），`parse_repo_ref` 直接判失败、不再落进裸名分支——否则 `github.com/owner` 会被当成 owner=`github.com` 的仓库去请求 GitHub，得到误导性的 404 而不是"地址无法识别"的 400。
- 保留页前缀清单作为模块内单一常量维护（当前含 settings／orgs／users／topics／features／marketplace／login／explore／trending／notifications／sponsors／codespaces／apps／collections／security／enterprise／account／organizations／events／readme／about／contact／site／billing／sessions／signup／teams／pricing／dashboard／pulls／issues／search／stars／new／discussions／password_reset 共 36 项）。清单**非穷尽**：GitHub 新增站内路由时按需增量补；未覆盖的两段路径会按 owner/repo 去请求 GitHub 并得到 404（而非 400），属已知取舍。扩清单只改该常量，不散落在路由里。
- 输入框 `maxlength` 取 300（给协议/域名前缀留余量），服务端仍按 140 上限判定，超限按 400 拒绝——浏览器约束只是体验，判定权在服务端。
- 备选：前端解析后只提交 `owner/repo` → 否。无 JS 时不可用，且同一套 URL 规则要在两端各写一份。
- 备选：复用 `routes._parse_full_name` → 否。它只认裸 `owner/repo` 且以 `HTTPException` 表达失败，属于 web 层；解析要覆盖多种地址形态并被单测直接调用，独立模块更干净，web 层只留状态码映射。
- 备选：引入 URL 归一第三方库 → 否。这是单站点（github.com）的形态归一，不是通用 URL 规范化，标准库 `urllib.parse` 加字符集校验已覆盖，引入依赖不减少边角。

### 2. 入口放 P6「我的关注」页，搜索页只挂一行引导

- 语义归属：动作是「把我的关注加上」，P6 是该功能的页面；地址输入框放元信息行下、标签筛选行上，与下方按语言分组的结果相邻，提交后新行即出现在下方。
- 搜索页的契约是自然语言检索：在搜索链路里加地址识别会产生「有时不检索」的第二条分支，破坏流式加载态与事件协议的确定性；且裸 `owner/repo`（如 react/vue）与自然语言词无法可靠区分。故搜索页只加一行表单外的静态提示（AI 不可用禁用表单时仍显示）。
- 备选：搜索框识别地址直出 → 否（裁定 1）；备选：顶栏加「＋关注」入口 → 否（顶栏 6 项已封顶，《交互流程说明》§1），且关注是 P6 的页内动作，不该占全局导航。

### 3. 写路径复用 `follow_repo` 三态，`POST /follows/add` 不碰库

- 路由职责固定三步：解析（失败 → 400 同页渲染）→ `await follow_repo(conn, client, normalized)` → 按结果映射（`already_followed` → `already`；否则取 `state` 的 joined/revived/alive）。
- 异常映射沿用既有口径：`GitHubNotFoundError` → 404；`GitHubAuthError` → 500（message 原样透传修复指引）；`GitHubError` → 502；`ValueError`（`ingest_followed_repo` 的 node_id 撞库/同名重建）→ 409（沿用既有文案）。
- 备选：新增 `add_follow_by_url` 服务函数把解析与写库包在一起 → 否。等于第二条写路径，回归面翻倍；解析与写路径分开才能各自独立测。

### 4. 大小写归一做进 `follow_repo`：NOCASE 查找＋规范名回传

- 查找改 `WHERE full_name COLLATE NOCASE = ?`（左操作数显式 COLLATE，语义无歧义；与右操作数写法实测等价）；命中时用库内 `full_name` 作为规范名，未命中走三态后续（`fetch_repo` 入池/复活后用 GitHub 返回的 `full_name`）。`FollowOutcome.full_name` 从此承载规范名，路由的 303 参数与横幅都用它。
- 这样幂等判定按库内身份而不是输入字符串：`PSF/Requests` 与 `psf/requests` 是同一仓库，不重复入池、不重复调用 GitHub。
- 备选：web 层先查库拿规范名再调 `follow_repo` → 否。两次查库，且「规范名」口径分裂在两层。
- 备选：应用层 `LOWER(full_name)` 比对 → 否。同样全表扫，还丢掉索引等值分支。
- 代价：`COLLATE NOCASE` 使该查询走全表扫（`repos` 约 6 万行级）。关注是人工单次操作，毫秒级可接受；本变更声明无 schema 变更，不新建 NOCASE 索引。

### 5. 单仓 GET 跟随 301，其余请求不动

- `fetch_repo` 调用 `_request(..., follow_redirects=True)`（httpx 请求级参数），只影响单仓元数据这一条 GET。
- 效果：旧链接 → 301 → 新名 → `item["full_name"]` 即新名 → 未入池时按新名入池；旧名在库且新名 node_id 已在另一条记录时，`ingest_followed_repo` 的既有 fail-loud 抛 `ValueError` → 409，库内 `full_name` 不变、不归并。
- 备选：客户端全局开 `follow_redirects` → 否。GraphQL/Search/README 不需要，全局打开会改变既有请求行为，回归面不必要地扩大。
- 备选：手动读 `Location` 再二次请求 → 否。等于手写重定向，还要处理循环与相对路径。

### 6. 成功走 PRG、失败同页渲染（HTTP 状态与错误类别一致）

- 成功：303 → `/follows?added=<规范名>&result=<joined|revived|alive|already>`；GET 渲染横幅，并按 `added` 查库取语言归类，算语言组锚点（`classify_language` → `f-<key>`，无语言 `other` → `#f-other`）。
- 失败：POST 直接以 400/404/409/500/502 渲染 `follows.html`，额外传错误条文案与输入原值；输入框回填原值（浏览器输入框上限 300 字内一律不截断）。
- 失败页的输入回显**不做字符截断**（spec「保留输入框原值」）：改为给请求体设上限 `_FOLLOW_ADD_BODY_MAX=8192` 字节，超限按固定短文案「输入过长」拒绝、不回显原文——正常表单（maxlength=300）永远触发不到该分支；这样「保留原值」与「防大字段灌页面」两个目标同时成立。带 `Content-Length` 的请求在读取 body **之前**就拒绝（不为超大请求分配内存），读出后按实际长度兜底（分块/缺长度场景）。
- 页面上下文收口：把 `follows_page` 的上下文构造抽成内部函数，GET 与 POST（成功后的 GET、失败时的同页）共用同一份元信息行/筛选 chips/分组/计数；GET 额外塞横幅字段，POST 失败额外塞错误字段。避免复制第二套分组与计数逻辑。
- 备选：失败也 303 回 GET 带错误参数 → 否。会把错误类别压成 200，与「HTTP 状态与错误类别一致」冲突，真实环境预演也没法按状态码核对。
- 备选：失败渲染独立错误页 → 否。用户要改的输入就在原页，跳走等于重来。
- 参数编码：用标准库 `urlencode` 生成查询串（`quote_plus`；full_name 无空格），GET 侧由框架解码后查库。
- 参数白名单：`result` 只接受 `joined`/`revived`/`alive`/`already` 四个值，其余值不渲染横幅（不给"已关注"兜底文案，避免伪造参数产生误导性结论）；`added` 必须通过 full_name 形态校验且在关注集内查到记录，否则整条横幅不渲染。

### 7. 不做清单（钉死边界）

不新增第二条关注写路径、不新增 JSON 接口；不做批量多地址、非 GitHub 域名（含企业版）、添加时打标签、改名自动归并、搜索框地址识别；不改采集、榜单、AI、库表与依赖。

### 8. 表单 POST 的同源校验（CSRF 防护）

- 新增的 `application/x-www-form-urlencoded` 表单 POST 属**跨站可提交的简单请求**（既有 JSON API 不是），在不改动全局鉴权（Caddy Basic Auth）的前提下补一层同源校验：请求带 `Origin` 时，其 hostname 必须等于请求 `Host` 或 `X-Forwarded-Host` 的 hostname（忽略端口与 scheme），否则 403；不带 `Origin` 时放行（同源表单/旧浏览器兼容）。403 是防御分支，返回 JSON 提示、不渲染 P6（跨站请求方不需要页面）；应用只监听回环，外部客户端无法直连伪造 `X-Forwarded-Host`，Caddy 默认透传 Host 且总是设置该头。
- 备选：引入 CSRF token/会话 → 否，项目无应用层会话与表单框架，单用户自用，同源校验已覆盖跨站表单这一唯一新增向量。
- 备选：全局中间件校验所有写端点 → 否，超出本变更范围，且既有 JSON API 不属简单请求。

## Risks / Trade-offs

- **改已验收写路径的回归面** → `follow_repo` 的改动只有「查找 SQL 加 NOCASE」与「回传规范名」两处，三态分支顺序与幂等短路位置不动；`tests/test_follows.py` 既有用例全部保留跑绿，新增大小写、规范名与 301 端到端用例；`POST /api/follows` 响应字段不变。
- **NOCASE 全表扫** → 约 6 万行级单次等值扫、毫秒级，且不在热路径（人工单次操作）；若将来体感变慢，另开任务建 NOCASE 索引（要迁移），本变更不做。
- **PRG 查询参数可伪造/过期** → 单用户自用，只影响展示、不改库内状态；但白名单**不能**保证说明与当前状态一致（手动改 `result` 可看到矛盾的 joined/revived 文案），这一点在 §21.4 明确写出并接受，不引入一次性票据。
- **表单 POST 的跨站面** → 同源校验（决策 8）挡住跨站表单提交；不带 `Origin` 的旧客户端放行是有意取舍（单用户）。
- **请求体上限** → 8192 字节；带 `Content-Length` 时在读取前拒绝，无长度时读后兜底；正常输入远小于此，超限只影响异常直连提交（400 固定文案，不回显）。
- **4xx/5xx 状态码渲染 HTML 页** → 页面类失败（400/404/409/500/502）照常展示 HTML 页，与既有页面路由的 404/500 行为一致；**除 403 跨站防御分支（JSON）外**不新增 JSON 错误体，也不改既有 API 的状态码语义。
- **失败同页渲染的上下文复制风险** → 抽内部上下文构造函数共用；用测试断言失败页仍含元信息行、关注计数与分组区，避免漏参导致页面残缺。
- **跟随 301 改变 `fetch_repo` 既有语义** → 只影响改名仓库（此前会把 301 响应体当元数据，属既有缺陷路径）；同名重建与正常仓库行为不变。测试补 MockTransport「301 → 200」用例与「旧名已跟踪 → 409」用例。
- **输入回显** → 错误条与输入框只以模板变量输出原值（Jinja 自动转义），不拼接 HTML；`added`/`result` 也不直接进 HTML，先经白名单与查库解析。
- **文案漂移** → 四类成功横幅与五类失败文案在 spec 中逐字钉死，测试断言关键子串；产品层《交互流程/21-直接关注.md》§21.4 与 spec 同源。
- **锚点组依赖语言归属** → 与 P6 分组同源（`classify_language`），无语言/未知语言归 `#f-other`（「其它语言」组），不新造分组、不出现锚点指向不存在的组。

## Migration Plan

1. 纯增量：代码（新解析模块＋两处服务端改动＋一个页面路由）、模板、样式；无 schema 变更、无数据迁移、无新增环境变量。
2. 本地按统一验证入口一次跑绿（`tools/verify.ps1`：ruff + pytest），并跑 `openspec validate direct-follow --strict` 与 `openspec validate --all --strict`。
3. 部署沿用既有流程（`docs/dev-commands.md` §4）；部署后 A 级真实环境预演：本地起服务＋真实 GitHub token，逐条跑「已入池 / 未入池 / 复活 / 已关注 / 400 / 404 / 409」七条路径，核对横幅、状态码、新行落组与顶栏计数。
4. 回滚：回滚代码即可。库内只可能出现正常关注数据（follows 行、入池仓库、基线快照），回滚后依然有效，无需清理；旧代码不认识任何新列/新表（本变更没有）。

## Open Questions

无。四类成功横幅与五类失败文案已在本 change 的 spec 中逐字定稿（与《交互流程/21-直接关注.md》§21.4 同源），实现按字面渲染；若后续要改文案，属文字层修改，spec 与 §21.4 同步即可，不改本设计的结构与边界。
