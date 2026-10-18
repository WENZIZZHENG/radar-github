# 提案：直接关注——粘贴仓库地址加入「我的关注」（direct-follow）

## Why

关注仓库的入口只有项目行右侧的 ☆（`app/web/templates/_row.html:18`），前提是该项目已经出现在榜单或搜索结果里；「我的关注」页（P6，`app/web/templates/follows.html`）没有添加入口，空态文案也只指向"去榜单点行右侧 ☆"。已知一个 GitHub 仓库地址、但它不在当期榜单/搜索命中里时，唯一可行路径是把地址粘进 P8 智能搜索，由 AI 自然语言链路（`app/search.py:430` `run_search`）把它当描述理解——结果不可控；AI 不可用时搜索页整页禁用表单（`app/web/templates/search.html:12`），这条路直接断掉。

本地库 alive 仓 10,681 个、关注 1 条：关注动作的实际瓶颈不在"选哪些"，而在"知道仓库后怎么把它加进来"。关注写路径（三态）与动态入池机制（《架构决策记录》决策 9）均已验收稳定，缺的只是一层地址解析与一个入口——不需要动数据、采集、榜单与 AI。

## What Changes

- **关注页直接关注入口**：P6「我的关注」页元信息行下、标签筛选行上提供一行地址输入框＋「关注」按钮；空关注页同样显示；原生表单提交，无 JS 可用。提交即关注，与行内 ☆ 同义，不调用 AI。
- **服务端统一地址解析**：新增单一解析模块，把输入归一为 `owner/repo`。接受：http/https 的 github.com / www.github.com 链接（可带子路径、查询串、片段、末尾斜杠、`.git` 后缀）、省略协议的 `github.com/owner/repo`、`git@github.com:owner/repo.git`、`ssh://git@github.com/owner/repo.git`；裸 `owner/repo` 只在关注页接受。拒绝：非 github.com 域名（含企业版）、只有 owner、GitHub 保留页前缀、含非法字符或超长——拒绝时如实提示。
- **新增页面路由 `POST /follows/add`**：唯一写路径复用既有三态关注写路径（已入池 alive 只补 follows 行／dead 复活＋基线快照／未入池抓单仓元数据入池＋基线快照）；不新增 JSON API。
- **大小写归一**：查库大小写不敏感命中，结果与横幅使用库内（已入池）或 GitHub 返回（新入池/复活）的规范名；已入池 0 次 GitHub 调用，未入池/复活 1 次。
- **结果反馈（PRG）**：成功 303 跳回 `/follows?added=<规范名>&result=<joined|revived|alive|already>`，GET 渲染四类成功横幅＋"在下方「{语言组}」组查看"锚点链接；失败不跳转，同页渲染原因并保留输入值，HTTP 状态与错误类别一致（400/404/409/500/502）。
- **改名链接**：单仓 GET 跟随 301 跳转；旧链接指向的仓库未入池时按新名入池；已跟踪（旧名在库、node_id 已存在）时 409 如实报告，不自动改写库内 `full_name`、不自动归并。
- **搜索页引导**：搜索框下加一行提示"已知仓库地址？到「我的关注」粘贴地址即可直接关注（不经过 AI 搜索）。"；不改搜索输入契约、检索链路与流式事件协议。

## Capabilities

### New Capabilities

- `direct-follow`: 已知 GitHub 仓库地址直接加入「我的关注」的完整行为——服务端地址解析与归一、关注页入口、四类成功横幅与五类失败反馈、复用三态关注写路径且不产生第二条写路径、搜索页一行引导不改检索行为、既有边界（不改名归并、不批量、不支持非 GitHub 域名、添加时不打标签）不扩大。

### Modified Capabilities

（无。搜索页只加一行静态引导提示，`smart-search` 的输入契约、检索链路与流式事件协议均不变；其余既有 capability 行为不变。）

## Impact

- **代码**：`app/reporef.py`（新建，地址解析与归一）、`app/follows.py`（大小写不敏感命中、回传规范名）、`app/collector/github.py`（单仓 GET 跟随 301）、`app/web/routes.py`（`POST /follows/add`、`GET /follows` 读 `added`/`result` 渲染横幅、失败同页渲染）、`app/web/templates/follows.html`（地址输入行＋结果横幅）、`app/web/templates/search.html`（一行引导）、`app/web/static/radar.css`（复用既有输入框/提示块样式，补成功态配色）；测试 `tests/test_reporef.py`（新建）、`tests/test_follows.py`、`tests/test_web.py`、`tests/test_github.py`。
- **接口**：新增一个 HTML 页面路由 `POST /follows/add`（成功 303，失败 400/404/409/500/502 渲染同页）；既有 `POST /api/follows`、`DELETE /api/follows/{full_name}` 的请求/响应契约不变。
- **数据**：无 schema 变更、无迁移；写库仍只经 follows 三态（follows 行、`repos(source='follow')`、基线快照）。
- **依赖**：无新增依赖（解析用标准库 `urllib.parse` 与正则）。
- **项目 SOP**：改用户可见口径（关注入口与结果反馈）→ 本 change 先行；文字层新增《交互流程/21-直接关注.md》专章（【待本人确认】）并回写《交互流程说明》《功能闭环清单》《任务拆解表》《README》；视觉为既有冻结视觉体系内组件复用，免示意图档。
