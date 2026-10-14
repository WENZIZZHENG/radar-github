# Tasks

> 前置：本人已确认《交互流程说明》§17 修订（流式加载态小节）与同保真原型 `docs/sop/原型-T041.html`。基线＝master ef70c79 之后（`_chat` 已含 `response_format` 透传）。

## 1. AI 客户端流式变体（app/ai.py）

- [x] 1.1 `DeepSeekClient` 新增 `_chat_stream`（async generator，`stream=True`）：逐 chunk 解析 `delta.reasoning_content`（str 增量优先）与 `delta.reasoning_details`（数组防御性提取：str 元素直取、dict 元素取 text/content 字符串字段、形态不识整体跳过）；`content` 增量累积为最终返回；首字节前失败允许按既有口径重试一次，已有增量下发后中断按调用失败抛既有异常类型；两字段皆缺 → 零思维链增量不报错。验证：`ruff check app/ai.py` 通过＋2.1/2.2 用例绿。
- [x] 1.2 `understand_intent`（app/ai.py:434）与 `select_and_reason`（app/ai.py:489）增可选 `thinking_cb` 参数：None（缺省）走既有 `_chat`（请求形态逐字节不变），非 None 走 `_chat_stream` 并把思维链增量喂回调；prompt/`response_format`/容错解析/返回契约一字不动。验证：2.3 用例绿＋既有 `tests/test_ai.py` 全绿（缺省路径零回归）。

## 2. 检索链路插桩（app/search.py）

- [x] 2.1 `run_search` 增可选 `progress_cb`（阶段事件）与 `thinking_cb`（透传两段 LLM 调用）：缺省 None 时函数行为与现状完全一致；补搜阶段如实区分"跳过（池内已足 20 条）/执行/失败跳过"。验证：2.4 用例绿＋`tests/test_search.py` 既有全绿。

## 3. 流式端点与模板（app/web/routes.py、app/web/templates/search.html、新增 partial）

- [x] 3.1 `search.html` 结果区块（`{% if search %}` 整块）抽为公共 partial（如 `_search_result.html`），同步模板 `{% include %}` 之——同步路径渲染输出逐字节不变。验证：3.1 后 `tests/test_web.py` 搜索相关用例全绿；`git diff` 确认同步模板仅搬迁不重写。
- [x] 3.2 新增 `POST /search/stream`（StreamingResponse，`application/x-ndjson`）：表单解析/长度校验复用同步路径同口径；生成器经 asyncio.Queue 旁路收集 `run_search` 回调事件按序 yield（NDJSON 行，`ensure_ascii=False`）；结束事件携带 SSR 结果片段（partial 渲染）＋prev_intent/prev_results 两个 JSON 值；`search.unavailable` 也走同一 partial 渲染下发。验证：2.5/2.6 用例绿。

## 4. 前端（app/web/static/radar.js、radar.css）

- [x] 4.1 radar.js 搜索提交改流式消费：特性检测（fetch＋ReadableStream，缺失不拦截走原生提交）→ preventDefault → 读流逐行 JSON.parse → 阶段步骤条三态/补搜跳过态渲染、思考区 `<details>` 两段流式追加（无思维链事件整区不渲染）→ 结果事件 innerHTML 整体替换结果容器＋写回追问隐藏字段；按钮置灰"搜索中…"现状保留。验证：人工页面走查对照原型。
- [x] 4.2 降级收敛：流中途失败（非 200/读流异常/结果事件未到即断）→ 失败提示＋表单原生提交自动重跑一次（再失败停留提示手动重试），不渲染半成品结果。验证：断网/杀进程模拟各一次留痕。
- [x] 4.3 进度面板/思考区样式从原型 `<style>` 迁入 radar.css（原型确认后），零外部资源。验证：`git diff` 确认仅追加样式段。

## 5. 单测

- [x] 2.1（tests/test_ai.py）流式变体：`reasoning_content` 增量逐段喂回调＋content 累积返回正确；`reasoning_details` 数组形态同效。验证：新用例绿。
- [x] 2.2（tests/test_ai.py）退化与错误链：两字段皆缺 → 零回调不报错；首字节前失败重试一次；增量下发后中断 → 抛既有异常类型。验证：新用例绿。
- [x] 2.3（tests/test_ai.py）缺省零回归：不传 `thinking_cb` 时 `understand_intent`/`select_and_reason` 走 `_chat`（payload 含 response_format、无 stream 字段）。验证：新用例绿＋既有全绿。
- [x] 2.4（tests/test_search.py）`run_search` 回调时序：阶段事件顺序＝意图起止→召回起止→精选起止→（补搜起止｜跳过）；精选失败/补搜失败路径事件如实；缺省 None 零事件。验证：新用例绿。
- [x] 2.5（tests/test_web.py）流式端点事件序列：NDJSON 逐行可解析、阶段/thinking/result 次序正确、result 事件 HTML 含意图透明行与结果行（`_row.html` 形态）、prev_intent/prev_results 值合法。验证：新用例绿。
- [x] 2.6（tests/test_web.py）两路径一致性：同一 stub 下流式 result 片段与同步 POST 结果页结果区一致（同 partial 渲染）；表单校验口径一致（空 q/超长 400）。验证：新用例绿。

## 6. 收口验证

- [x] 6.0 走查打回修复（2026-10-13 本人走查）：「思考过程不是实时的」——诊断四路对照实证端到端流式本身实时（httpx 增量平均 27ms、浏览器裸 fetch 平滑），体感卡顿来自网关/模型首 token 静默与抖动（实测 2s~60s，不可控；本人走查轮撞上一次 APITimeoutError 60s 超时重试）。修复＝进行中步骤显示已用时秒数每秒跳动（纯前端计时器，静默期存活信号）；spec「搜索流式加载态」需求与场景已同步该口径。验证：agent-872 实施，变异验证锁非空转、真网关冒烟计时逐秒跳动（间隔 0.91~1.23s）、`pytest tests/test_search.py tests/test_ai.py` 153 绿＋全量 575 绿＋ruff 绿；截图 `data/t041_rehearse/fix-timer-*.png`。
- [x] 6.1 统一验证入口一次跑绿：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1`（ruff＋pytest 全量），输出留痕。——2026-10-13 跑绿：ruff All checks passed＋pytest 574 passed in 107s。
- [x] 6.2 独立评审子 agent 复审（核心链路搜索变更，按项目 §4 评审门禁），findings 处置闭环。——2026-10-13 评审 agent-859（k3）两轮：初审 1F1＋2F2＋3F3，修复后增量复查全部清零判可收口；唯一留痕不修＝`failSearchStream` catch 分支文案矛盾（极端不可达角落，F3 级）。
- [x] 6.3 A 级真实环境预演（真 uvicorn＋真 AI 网关流式调用）：页面走查对照原型 T041——阶段推进、思维链流式（或如实退化）、结果与同步路径一致、断流降级重跑；留档 data/。——2026-10-13 预演通过（agent-866 脚本段两轮 5/5 PASS＋主 agent 看图段 7 张截图全过）：真网关 `deepseek/deepseek-v4.1-flash`＋json_object 下思维链非空（114 事件/1128 字符，by_stage intent 46/select 68），**推翻 10-12 实测「json_object 下思维链近零」推论**，生产思维链区应正常渲染；阶段序列 intent→recall→select→external 次序正确；DOM 终态与 result 帧逐值一致；隐藏字段动态创建且与帧值逐字节相同；断流降级实证收敛（stream abort→原生 POST 整页 SSR）；产物 `data/t041_rehearse/`（rehearse.py/server.py/result.json/ndjson 抓包×2/截图×7/步骤卡.md）。

## 7. 部署后留痕（生产）

- [x] 7.1 生产真实流式搜索一次，留痕：流式＋json_object 组合下 `reasoning_content` 是否非空（决策 5 存疑点了结）、事件序列完整、耗时对比；若思维链确为空如实记录（按拍板退化为纯进度，是否放宽协议参数另行拍板）。——**实测（2026-10-14 生产部署后，服务器回环 POST /search/stream，q="我要做爬虫，Python 的"）**：共 303 帧＝8 stage＋**294 thinking**＋1 result；**`reasoning_content` 非空、逐段透出**（intent 897 字符、select 2632 字符，多帧增量而非流末一次性——决策 5 存疑点了结：透出）；阶段序列完整 intent→recall→select→external 四段 start/done 齐全；result 帧 HTML 49239 字符；端到端约 30s（含 ssh 链路开销）。注：本次部署已含 `ai-langchain-refactor`（思维链走 extra_body 下发 response_format 的修复态），抓包留档本地 `data/prod-deploy-20261014/stream.ndjson`。
- [x] 7.2 回环冒烟（/、/search 及一次流式搜索）全 200；回写《验收记录.md》或等价留痕位置。——**实测（2026-10-14）**：服务器回环 `/`、`/?board=all`、`/total`、`/total?board=all`、`/search`、`/follows`、`/tags` 全 200；流式搜索见 7.1（303 帧正常收尾）；公网 `https://radar.suipaiji.com/` 未授权 401（鉴权墙正常）。回写见《验收记录.md》2026-10-14 条。

## Workflow follow-up

- 验收通过后 `openspec archive streaming-search-progress` 归档（specs delta 沉淀进 `openspec/specs/smart-search/spec.md`）。
- 产品层回写：《交互流程说明》§17 修订状态由"待本人确认"改确认；《任务拆解表》T-041 状态流转并当场归档。
