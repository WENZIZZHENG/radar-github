# Design：搜索流式进度与思维链显示

## Context

现状与动机见 proposal.md。与本设计直接相关的现状约束（均已读码核实）：

- **搜索现语义是表单 POST 同步 SSR，不是 GET**：`search.html:11` `method="post" action="/search"` → `routes.py:1730` `@router.post("/search")`（urlencoded 手动解析，不引 python-multipart）。任务背景中"搜索现语义是 GET 表单 q 参数"的描述与代码现状不符，本设计以代码为准。**搜索结果页当前本就不经 URL 分享**（POST 不落在地址栏），"URL 可分享性"不是需要保留的既有行为。
- 检索链路唯一入口 `run_search`（app/search.py:416）：意图理解 → `recall_candidates`（毫秒级 SQL）→ `_select_with_ai` →（不足 20）`search_external`；降级姿态分段钉死（意图失败 → unavailable；精选失败 → 粗排直出；补搜失败 → 标注跳过）。
- AI 调用唯一出口 `_chat`（app/ai.py:519），已有 `response_format` 可选透传（rebase 点：master ef70c79，`ai-json-response-format` 已合并部署）；意图理解/精选调用均带 `response_format={"type":"json_object"}`。
- 结果行唯一形态 `_row.html`；`_search_view`（routes.py:1603）装配行视图；radar.js 行交互（折叠/关注/打标）全部走 `document` 级事件委托（radar.js:133/182 等），动态注入的 HTML 片段无需重新绑定即可交互。
- 生产思维链字段 `reasoning_content`（字符串增量，实测）；本地 free 模型为 `reasoning_details`（数组形态，结构未完全实测）。
- **2026-10-12 生产实测留痕**：`json_object` 模式下 reasoning tokens 近零（3.0s vs 默认 22.7s/1454 tokens）——思维链在协议层 JSON 约束下可能为空（见决策 5 与风险）。

## Goals / Non-Goals

**Goals:**

- 搜索等待期间用户实时可见：分阶段进度（四阶段步骤条）＋ AI 思维链英文原文流式追加。
- 结果语义唯一：流式/同步两路径同 `run_search`、同结果区模板，结果内容/形态一致。
- 降级收敛唯一：任何流式不可用/失败场景收敛回既有同步表单 POST，无第三条结果形态。
- 行形态单一事实源不破：结果片段服务端 SSR（复用 `_row.html`），前端零行模板。

**Non-Goals:**

- 不改检索链路任何语义（意图透明行、degraded、精选兜底、补搜标注、空结果、追问合并/搜空保留上一轮——一字不动）。
- 不建搜索缓存；不引前端框架/构建工具/新依赖（vanilla JS＋openai SDK 既有 `stream=True`）。
- 不做搜索取消按钮（AbortController 由页面导航天然触发；显式取消入口属新交互，留待本人另行拍板）。
- 不做"思考过程"翻译/润色（英文原文如实显示，不承诺中文）。
- 不改同步路径请求形态（`_chat` 缺省路径逐字节不变）。

## Decisions

### 决策 1：POST＋fetch ReadableStream（否 GET＋EventSource）

结论：**`POST /search/stream`，前端 fetch＋`response.body.getReader()` 手动消费**。论证：

1. **追问上下文体积**：无状态回传口径要求每轮携带 `prev_intent`（JSON）与 `prev_results`（至多 20 行摘要，实测约 5~15KB）。EventSource 只能 GET——把这些塞进 query string 会撞到 URL 长度实际上限（浏览器/代理/Caddy 链路 8KB 级即不稳），且意图 JSON 上地址栏丑陋易碎；改服务端存会话则直接违反 spec"追问上下文无状态回传"钉死口径。POST body 与现同步路径字段形态完全一致，零新概念。
2. **EventSource 自动重连是有害语义**：连接闪断时 EventSource 默认带 Last-Event-ID 重连——本场景重连＝服务端整链重跑＝重复烧 LLM 调用（单次搜索 1~3 次 DeepSeek 调用，成本口径 §17.4 钉死）。要防就得自造 idempotent resume 协议，复杂度远超手动读流。fetch＋AbortController 行为完全显式：断了就是失败事件，按决策 7 收敛。
3. **与现状同构**：现语义本即 POST 表单（见 Context 纠错），流式端点镜像同一 method＋字段集，两条路径只差响应形态，语义唯一性论证最简。
4. 代价（可读流手动解析）由决策 2 的帧格式选型压到最低；浏览器支持面（fetch＋ReadableStream）覆盖项目目标浏览器，缺失即走降级路径（决策 7）。

### 决策 2：帧格式 NDJSON（每行一个 JSON 事件对象）

传输形态按拍板为服务端流式推送（FastAPI StreamingResponse）；既已选 POST＋fetch（决策 1），EventSource 协议帧（`event:`/`data:` 行）不再免费可得，需二选一：

- **NDJSON（选用）**：每行一个 JSON 对象 `{"type": ..., ...}`，`JSON.stringify` 自动转义内嵌换行（思维链文本/HTML 片段都是字符串值），前端 `split("\n")`＋`JSON.parse` 即完事；HTML 片段经 JSON 字符串传输无注入歧义。
- 手写 SSE 帧解析：需处理多行 data 拼接、事件分界空行，代码量与出错面都更大，且没有 EventSource 在消费——纯负资产。

响应 `Content-Type: application/x-ndjson`；服务端生成器逐事件 `yield json.dumps(event, ensure_ascii=False) + "\n"`（`ensure_ascii=False` 保中文可读，与项目既有 `json.dumps(..., ensure_ascii=False)` 惯例一致）。

### 决策 3：结果下发＝SSR HTML 片段（前端零行模板）

流式结束事件携带**服务端渲染好的整个结果区 HTML 片段**，前端 `innerHTML` 整体替换结果容器：

- 模板单一事实源：`search.html` 的结果区块（意图透明行/结果列表/补搜标注/追问对比/上一轮区，即现 `{% if search %}` 块）抽为公共 partial（如 `_search_result.html`），同步路由模板 `{% include %}` 之、流式端点 `templates.get_template(...).render(...)` 之——两条路径渲染同一段模板代码，"结果语义唯一"由构造保证而非靠约定。
- 行形态不破：partial 内部照旧 `{% include "_row.html" %}`（anchor "s"/"p" 口径不变），前端不重复实现行模板（裁定 5 倾向落地）。
- 交互零重绑：radar.js 行交互走 document 级事件委托（Context 已核实），innerHTML 注入后折叠/关注星/打标即活；追问隐藏字段（prev_intent/prev_results）不能靠片段（它们在表单内、结果容器外），由结果事件同包携带两个 JSON 字符串值，前端写入既有 hidden input——无状态回传机制本身一字不改。
- 备选（下发结构化数据＋前端拼行）：否决——前端被迫复制 `_row.html` 全部分支（池外徽标/端点日期/标签/概要/理由块），单一事实源立刻破，且每次行模板演进都要双端同步。

### 决策 4：AI 客户端流式变体——`_chat_stream`＋可选思维链回调

- `DeepSeekClient` 新增 `_chat_stream(...)`（async generator）：`chat.completions.create(..., stream=True)`，逐 chunk 解析 `delta`——`reasoning_content`（str，直接作为增量）优先；缺则探 `reasoning_details`（数组，防御性提取：元素为 str 取之、为 dict 取其 `text`/`content` 字符串字段，形态不识整体跳过）；`content` 增量累积为最终返回内容。两字段皆缺 → 不产生任何思维链增量（退化为纯阶段进度，决策 4 拍板口径）。
- `understand_intent`/`select_and_reason` 增可选参数 `thinking_cb: Callable[[str], None] | None = None`：为 None（缺省）时走既有 `_chat`（同步路径请求形态逐字节不变）；非 None 时走 `_chat_stream` 并把每个思维链增量喂给回调。两段调用的 prompt、`response_format`、容错解析（`_parse_suggested_topics` 第二道防线）、返回值契约一律不变。
- **错误链姿态**：流式调用仅在"首字节前失败"允许按既有口径重试一次（无任何增量已下发，重试无副作用）；一旦已有增量下发后中断，按调用失败处理——意图理解段 → "搜索暂不可用"（fail-loud），精选段 → 退化粗排直出（与同步路径同语义，spec 降级口径不破）。
- 思维链语言：实测为英文（prompt 全中文也一样），如实显示原文，不做翻译/摘要。

### 决策 5：流式调用保留 `response_format={"type":"json_object"}`（存疑点登记）

流式变体与同步路径使用**同一协议参数集**（含 json_object）——两道容错防线的拍板口径（`ai-json-response-format`）不因传输形态改变而松动。**已知风险**：2026-10-12 实测 json_object 模式下 reasoning tokens 近零，即生产环境两段调用很可能**没有思维链可显示**，B 档目标②退化为纯阶段进度。本设计按拍板口径接受该退化（决策 4 拍板："缺则无思维链只显阶段进度"），并把"生产实测流式＋json_object 下 reasoning_content 是否非空"列为部署后留痕任务；若确为空且本人希望要思维链，再拍板"流式调用是否放宽 json_object"（属新 change，不在本变更内擅自放宽）。

### 决策 6：`run_search` 插桩＝可选回调，缺省零行为变化

`run_search` 增可选参数 `progress_cb`（阶段事件）与透传的 `thinking_cb`：缺省 None 时函数体行为与现状完全一致（同步路由不传参，零回归面）；流式端点传入回调，在四个阶段边界（意图理解起止/召回起止/精选起止/补搜起止）与思维链增量处产生事件。补搜阶段的"跳过（池内已足 20 条）"与"失败跳过"由 `pool_hits` 数量与 `external_failed` 如实映射，不伪造进度。阶段事件在生成器内排队（asyncio.Queue）后按序 yield——检索链路串行本质不变，事件只是旁路观察。

### 决策 7：降级收敛——三级判定全部回到同步表单 POST

1. **无 JS**：表单原生提交 → 同步 POST SSR（现状行为，天然成立）。
2. **无流式能力**：radar.js 特性检测 `window.fetch`＋`ReadableStream` 缺失时不拦截 submit（现状行为原样保留）。
3. **流中途失败**（HTTP 非 200/读流异常/结果事件未到连接即断）：进度区替换为失败提示＋自动以表单原生提交重跑同一查询（用户看到的就是"卡了一下后进结果页"），不渲染半成品结果。重跑会重烧一轮 LLM 调用——失败是低频异常路径，成本可接受；为防连环失败，自动重跑仅一次，再失败则停留提示由用户手动重试。

### 决策 8：进度面板形态（以原型 T041 为视觉基线）

- 阶段步骤条：四步横排（窄屏纵排），三态＝未开始（灰）/进行中（品牌绿＋呼吸点）/已完成（绿勾）；补搜步第四态＝跳过（灰字"无需补搜"）。
- "AI 思考过程"区：`<details>` 元素，流式期间默认展开、两段调用各一节（节头"意图理解"/"精选"），等宽字体英文原文流式追加并自动滚到底；完成后保留展开态由用户自行折叠（拍板 4"完成后可折叠"＝可折叠能力，非强制自动收起）。无任何思维链增量时整区不渲染（纯步骤条）。
- 按钮态沿用现状：提交即置灰"搜索中…"（既有文案不动），完成/失败后恢复。
- 新视觉元素（步骤条/思考区）样式先进原型文件 `<style>`，本人确认后迁 radar.css（项目惯例，原型 T021 同模式）。

## 路径唯一性自查

- **几条路径**：搜索执行共两条——①同步 `POST /search`（SSR 整页，无 JS/降级路径）；②流式 `POST /search/stream`（JS 默认路径）。页面入口 `GET /search` 两条共用（空页渲染，不变）。
- **语义是否唯一**：唯一。两条路径跑同一 `run_search`（决策 6 缺省零差异）、渲染同一结果区 partial（决策 3 构造级同源）；差异仅在传输与加载态——流式多"过程可见"，结果内容/形态/降级口径/追问无状态回传完全一致。无 JS 时②不存在；流式失败时②收敛回①（决策 7），任何时刻用户只会看到一种结果形态。
- **降级如何收敛**：三级判定（无 JS → 原生提交；无流式能力 → 不拦截提交；流中途失败 → 提示＋原生提交重跑一次）全部汇入①，无第三条形态。
- 入口/注册/登录/权限/删除/数据归属：均不涉及。

## Risks / Trade-offs

- [json_object 下思维链可能为空（2026-10-12 实测 reasoning tokens 近零）→ B 档目标②在生产退化为纯阶段进度] → 按拍板口径接受退化（决策 5）；部署后生产实测留痕，确为空再议是否放宽协议参数（新 change 拍板）。阶段进度（目标①）不受此影响。
- [本地 free 模型 `reasoning_details` 数组结构未完全实测 → 解析可能拿不到文本] → 防御性解析＋整体跳过（决策 4），退化为纯进度不报错；本地实测结果如实留痕。
- [流式＋json_object 组合在网关侧未实测 → 可能 400 或行为异常] → 按既有错误链处理（不自动去参重发，与既有拍板一致）；实施任务含本地/生产真实流式调用验证。
- [流式连接长时间占用（10~30s）× uvicorn 单 worker → 并发搜索互相排队] → 单人使用规模档 S，可接受；StreamingResponse 为 async generator 不阻塞事件循环，日常榜单浏览不受影响。
- [结果片段 innerHTML 注入的 XSS 面] → 片段内容与同步 SSR 完全同源（Jinja 自动转义照常），不引入新的注入面；前端不使用 `insertAdjacentHTML` 之外的解析路径。
- [流式期间用户再次提交/双击] → 按钮置灰 disabled 防连点（现状机制保留）；导航离开由浏览器自动中断 fetch。

## Migration Plan

无迁移：无 schema/配置/数据变更。部署＝代码上线重启服务；回滚＝回退 commit 重启（流式端点为纯新增，同步路径代码缺省形态逐字节不变，回滚零残留）。部署后按 tasks.md 执行生产真实流式调用留痕（含 reasoning_content 非空判定）。
