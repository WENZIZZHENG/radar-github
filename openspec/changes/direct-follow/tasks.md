# 任务：直接关注（direct-follow）

## 1. 地址解析层（服务端单一模块）

- [x] 1.1 新建 `app/reporef.py`：把输入归一为 `owner/repo`——接受 http/https 的 github.com / www.github.com 链接（可带子路径、查询串、片段、末尾斜杠、`.git` 后缀）、省略协议的 `github.com/owner/repo`、`git@github.com:owner/repo.git`、`ssh://git@github.com/owner/repo.git`；验证：`tests/test_reporef.py` 覆盖 spec「接受」两类场景，逐形态断言归一结果
- [x] 1.2 `app/reporef.py`：拒绝路径——非 github.com 域名（含企业版与 `notgithub.com` 这类近似域）、只有 owner、GitHub 保留页前缀（settings/orgs/users/topics/features/marketplace 等，单一常量维护）、非法字符与超长（owner 39＋repo 100，上限 140）；验证：`tests/test_reporef.py` 断言各形态返回失败且不产生任何外呼
- [x] 1.3 `app/reporef.py`：两个入口分工——`parse_github_ref` 只认地址形态（将来其它调用方复用），`parse_repo_ref` 在其上接受裸 `owner/repo`（关注页用）；失败统一返回 `None`（失败原因只进日志与测试断言，不扩大公开返回形态）；验证：单测断言 `psf/requests` 在 `parse_repo_ref` 通过、在 `parse_github_ref` 拒绝
- [x] 1.4 `tests/test_reporef.py`（新建）：参数化矩阵（每种接受形态 × 大小写/`.git`/末尾斜杠组合；每种拒绝形态；空串与纯空白）；验证：`python -m pytest tests/test_reporef.py` 全绿

## 2. 关注写路径归一（大小写与 301）

- [x] 2.1 `app/follows.py`：`follow_repo` 的库内查找改大小写不敏感（`COLLATE NOCASE`），命中时以库内 `full_name` 作为规范名回传；未入池/复活以 GitHub 返回的 `full_name` 为规范名；三态分支顺序与幂等短路位置不动；验证：`python -m pytest tests/test_follows.py` 既有 21 个用例保持绿
- [x] 2.2 `tests/test_follows.py`：补用例——`PSF/Requests` 命中库内 `psf/requests`（0 次 GitHub 调用、回传库内规范名）、大小写变体下幂等短路仍准确（already_followed=True、库内零写入）；验证：新增用例绿
- [x] 2.3 `app/collector/github.py`：`fetch_repo` 请求级打开跟随重定向（只这一条 GET，客户端全局与 GraphQL/Search/README 不动）；验证：`tests/test_github.py` 新增 MockTransport「301 → 200」用例，断言取到新名与元数据
- [x] 2.4 `tests/test_follows.py`：补「旧链接指向未入池仓库 → 跟随 301 后按新名入池」与「旧名在库且新名 node_id 已存在 → `ValueError`（409 口径）且库内 `full_name` 不被改写」；验证：新增用例绿

## 3. 关注页入口、路由与反馈

- [x] 3.1 `app/web/routes.py`：把 P6 页面上下文构造抽成内部函数供 GET 与失败分支共用；`follows_page` 读 `added`/`result`（`result` 白名单，`added` 查库解析语言组锚点）渲染成功横幅；验证：`python -m pytest tests/test_web.py -k follows` 既有用例保持绿
- [x] 3.2 `app/web/routes.py`：新增 `POST /follows/add`——读表单地址 → 调 `app.reporef` 解析（失败 400 同页渲染）→ 调既有 `follow_repo`（不新增写路径）→ 成功 303 到 `/follows?added=<规范名>&result=<joined|revived|alive|already>`；异常映射 404/409/500/502，失败一律同页渲染并回填输入；同源 `Origin` 校验（与 Host / X-Forwarded-Host 比较，跨站 403）与请求体上限 8KB（带 Content-Length 时读前拒绝，超限 400 不回显）；验证：`tests/test_follows.py` 新增用例覆盖成功四态、400/404/409/500/502、跨站 403（含反代 Host 改写）与超限 400（`app.dependency_overrides` 换假 client）
- [x] 3.3 `app/web/templates/follows.html`：元信息行下、标签筛选行上加地址输入行（原生 `<form method="post" action="/follows/add">`＋输入框＋「关注」按钮，`label`/`aria-label` 齐备，`maxlength="300"`）＋口径提示行；空关注态同样显示；补失败错误条（含输入回填）与成功横幅（含「在下方「{语言组}」组查看」锚点链接）；验证：`tests/test_follows.py`/`tests/test_web.py` 断言空态也含输入框、四类横幅文案、锚点 `#f-<key>` 与 `#f-other`
- [x] 3.4 `app/web/static/radar.css`：复用既有 `.search-box` 与 `.notice` 形态补输入行与横幅的成功/错误配色（不新增视觉元素、不引入新设计语言）；验证：本地起服务页面截图走查输入行、横幅、锚点跳转与移动端不溢出
- [x] 3.5 `tests/test_follows.py`：补页面级断言——四类成功横幅逐字、锚点链接、新行落入对应语言分组（无增量标"—— 下周起有数据"）、顶栏关注计数同步、失败页保留输入且状态码正确；验证：`python -m pytest tests/test_follows.py -q` 全绿
- [x] 3.6 `tests/test_follows.py`：断言失败渲染的仍是完整 P6 页面（元信息行、关注计数、分组区都在），不是残缺片段；验证：对 400/404 两条路径断言页面骨架元素

## 4. 搜索页引导提示

- [x] 4.1 `app/web/templates/search.html`：搜索表单之后加一行静态引导"已知仓库地址？到「我的关注」粘贴地址即可直接关注（不经过 AI 搜索）。"（链接 `/follows`），AI 不可用禁用表单时照常显示；验证：`tests/test_search.py` 断言提示存在、AI 不可用页面同样存在
- [x] 4.2 `tests/test_search.py`：回归断言搜索输入契约与检索链路未变（表单 `action="/search"`、输入字段仍为 `q`、隐藏字段 `prev_intent`/`prev_results` 逻辑不变、流式入口路由仍在）；验证：相关用例绿

## 5. 文档回写（产品层）

- [x] 5.1 `docs/sop/交互流程/21-直接关注.md`（新建）：按 21.1~21.7 结构成文，结果横幅与失败文案与 spec 逐字一致；确认状态【待本人确认】、版本 v1、日期 2026-10-18；验证：与 `openspec/changes/direct-follow/specs/direct-follow/spec.md` 文案逐条比对无差异
- [x] 5.2 `docs/sop/交互流程说明.md`：头部版本 v3.2 与起草说明、§1 站点地图 P6 行、§2A 第 1 层前的添加关注行、§3.2 第 0 步、§4 分支表四行、专章索引 §21 行；验证：人工比对六处改动点，未改动其他章节
- [x] 5.3 `docs/sop/功能闭环清单.md`：功能 2「关注」行的入口/操作/结果三列按新入口补写；验证：人工比对三列文案
- [x] 5.4 `docs/sop/任务拆解表.md`：当前速览加批次十七条目、主表加 T-043 行、详情卡（T-040 卡之后）填已确认事实/不改什么/验证与评审/commit 占位；验证：人工比对行内字段齐全、风险级与验证方式与高风险口径一致
- [x] 5.5 `README.md`：第 12 行「关注与标签」补"页内粘贴 GitHub 地址直接关注"；验证：人工比对一行文案

## 6. 验证与收口

- [x] 6.1 `tools/verify.ps1` 一次跑绿（ruff + pytest；基线 586 passed＋本变更新增用例），留存日志与退出码
- [x] 6.2 `openspec validate direct-follow --strict` 通过；`openspec validate --all --strict` 的 3 项存量失败（ai-refresh-cadence 的 Purpose 占位、local-ai-relay 与 smart-search 各超长 requirement）不在本变更范围，留痕不修（留存原始输出与退出码）
- [x] 6.3 A 级真实环境预演（本地 uvicorn＋真实 `GITHUB_TOKEN`）：逐条跑「已入池 / 未入池 / 复活 / 已关注 / 400 / 404 / 409」七条路径，核对状态码、横幅文案、GitHub 调用次数（0 与 1）、新行落组与顶栏计数，留档执行记录
- [ ] 6.4 页面走查（真实页面截图）：P6 输入行与空态、四类成功横幅与锚点跳转、五类失败同页渲染与输入保留、搜索页引导；按 `docs/sop/复验交付规范.md` 出复验步骤卡
- [ ] 6.5 本人页面验收通过后（另行一轮）将本 change 归档并同步 `openspec/specs/`（`openspec archive direct-follow`），确认 `direct-follow` 能力落入主规格库；本轮收口＝统一验证入口一次跑绿＋独立评审通过＋commit
