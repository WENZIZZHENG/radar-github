# Tasks

## 1. 实现（app/ai.py）

- [x] 1.1 `DeepSeekClient._chat` 新增可选关键字参数 `response_format: dict | None = None`，非 None 时透传给 `chat.completions.create`、None 时不传该字段（纯文本方法请求体逐字节不变）；验证：`pytest tests/test_ai.py -k "posts_chat_completions_payload or retries or fails_fast or auth_error or content_policy or malformed or timeout or empty_key"` 既有用例全绿（缺省路径零回归）。
- [x] 1.2 `suggest_topics`（app/ai.py:407）、`understand_intent`（app/ai.py:429）、`select_and_reason`（app/ai.py:479）三处 `_chat` 调用点传 `response_format={"type": "json_object"}`；prompt、`_parse_suggested_topics`、各降级路径一字不动；验证：`ruff check app/ai.py` 通过，且 `git diff` 确认改动仅限 `_chat` 签名/透传与三处调用点。

## 2. 单测（tests/test_ai.py）

- [x] 2.1 新增断言三方法请求 payload 含 `response_format={"type":"json_object"}`（参照 `test_client_posts_chat_completions_payload` 的 stub 姿态，逐方法断言 captured payload）；验证：三条新用例绿。
- [x] 2.2 新增断言纯文本方法（translate 或 recommend 任一）payload 不含 `response_format` 字段；验证：新用例绿。
- [x] 2.3 容错解析第二道防线回归：三方法各自的 stub 返回带 markdown 围栏/前后缀杂质的合法 JSON，断言解析结果正确（证明协议层之下容错路径仍生效）；验证：新用例绿。
- [x] 2.4 400 不自动回退：stub 对带 `response_format` 的请求报 400（`APIStatusError`），断言 client 抛出既有异常类型且**未发起第二次请求**（request 计数 == 1，无去参重发）；验证：新用例绿。
- [x] 2.5 降级语义回归：stub 返回非法 JSON 时 `understand_intent`/`select_and_reason` 仍返回 None、`suggest_topics` 仍抛 `DeepSeekError`（对外行为不变）；若既有用例已覆盖则指认用例名并在 PR 说明，不重复造；验证：相关用例绿。

## 3. 收口验证

- [x] 3.1 统一验证入口一次跑绿：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1`（ruff + pytest 全量），输出留痕（实施方跑绿：`All checks passed!` / `549 passed in 129.03s`，agent-829；评审亲跑 `pytest tests/test_ai.py` 78 passed 复核）。
- [x] 3.2 独立评审子 agent 复审（核心链路 AI 调用变更，按项目 §4 评审门禁），findings 处置闭环（agent-837：F1=0/F2=0/F3×3 全留痕不修，判可收口）。

## 4. 部署后留痕（生产）

- [x] 4.1 部署重启后在生产对 `understand_intent`（或 `suggest_topics`）发起一次真实调用，留痕：请求带 `response_format`、HTTP 200、返回合法 JSON、耗时/token 对比默认调用；记录进《验收记录.md》或等价留痕位置。**留痕（2026-10-13，本行为等价留痕位置）**：部署 ef70c79 后服务器真实调用 `understand_intent("我要做爬虫")`（deepseek/deepseek-v4.1-flash，commandcode 网关，经 `_chat` 新代码路径带 json_object）→ **3.8s** 返回合法 JSON `{"keywords": ["crawler","spider","scraping","web-scraping","crawl"], "languages": [], "topics": [...], "filters": {}}`，契约字段齐全；对比同网关默认调用实测 22.7s/1454 reasoning tokens（2026-10-12 留痕），提速约 6 倍。回环冒烟 /、/?board=all、/total、/total?board=all、/follows、/search 全 200。
- [x] 4.2 若本地 .env.dev free 模型（stealth/glyph-cluster:free）顺手实测该参数，结果如实记录；实测 400 不视为验证失败（拍板口径：不自动回退，换提供方时再议）。**留痕（2026-10-13）**：未实测——该 free 模型上游 2026-10-12 起持续 408（"Upstream model provider is temporarily unavailable"），按拍板口径不视为验证失败；本地开发期该模型恢复后若对 response_format 报 400，按决策 2 走既有错误链、不自动回退。

## Workflow follow-up

- 验收通过后 `openspec archive ai-json-response-format` 归档（specs delta 沉淀进 `openspec/specs/smart-search/spec.md`）。
- 本变更零产品层口径变化，`docs/sop/` 冻结物无需回写。
