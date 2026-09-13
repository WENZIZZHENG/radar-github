# 任务：本地 AI 导出/回填通道（local-ai-relay）

## 1. 数据层与迁移

- [ ] 1.1 `app/schema.sql`：`recommendations` 增加 `source TEXT NOT NULL DEFAULT 'ai'`
- [ ] 1.2 `app/db.py`：新增 `_migrate_recommendations_source`（`PRAGMA table_info` 查列存在 → `ALTER TABLE ADD COLUMN`，幂等）并接入 `init_db` 迁移链
- [ ] 1.3 `tests/test_db.py`：补迁移用例（新列存在、存量行默认 `ai`、重复执行幂等）

## 2. prompt 构造抽取（导出与 AI 共用一份）

- [ ] 2.1 `app/ai.py`：抽 `build_recommend_prompt` / `build_summary_prompt` / `build_translate_prompt` 纯函数，`DeepSeekClient` 三个方法改为调用
- [ ] 2.2 `tests/test_ai.py`：加回归用例——抽取后的 system/user 与既有构造逐字一致（含 week/quarter 增量行、total 无数字、rising 入池语境分支）

## 3. 导出判定与作业单

- [ ] 3.1 新模块 `app/local_ai.py`：导出判定（复用 `_scope_sets` + translate/week/quarter/total/summary 五段缺失与窗口判定 + `source='manual'` 行不列为重生任务）
- [ ] 3.2 同上：README 拉取复用 `_ReadmeState`（按 `AI_README_HEAD_CHARS` 截断、失败退化元数据）
- [ ] 3.3 同上：作业单渲染两形态（`format=text` 自包含作业单含输出格式要求；`format=json` 结构化清单含 `as_of`/`week_label`/`quarter_label`/`window_open`/`remaining`），两形态任务集合一致
- [ ] 3.4 `tests/test_local_ai.py`：范围与判定用例（非窗口日只列缺失、窗口日列重生、manual 行排除、kind 过滤、limit 截断与 remaining、README 失败退化）

## 4. 回填写入

- [ ] 4.1 `app/local_ai.py`：解析与逐条校验（必填字段、仓库存在、kind 合法、期次与当前一致、文本清洗后非空且不超长），错误入 `errors` 不阻断同批
- [ ] 4.2 同上：最小清洗（去首尾空白、成对引号、"推荐理由：/推荐语：/译文："类前缀）
- [ ] 4.3 同上：写入语义（默认只补缺、`overwrite=true` 覆盖、一律写 `source='manual'`；week/quarter 写 `generated_week`+`readme_sha=NULL`；total/summary 回填时重取 sha；translate 走 `description_zh IS NULL AND description_en = ?` 护栏；单条 commit）
- [ ] 4.4 `tests/test_local_ai.py`：写入分支用例（默认不覆盖、显式覆盖、重复回填幂等、期次过期拒绝、仓库不存在、空文本/超长、translate 护栏、total/summary 指纹写入）

## 5. 自动路径的人工行豁免

- [ ] 5.1 `app/ai.py`：`recommend_missing` 的 quarter/total/summary 三处窗口重生守卫增加 `source='manual'` 跳过（不拉 README、不比 sha、不调用 AI）
- [ ] 5.2 `tests/test_ai.py`：补用例（窗口日人工行不重生且指纹不变、同仓缺失维度照常补缺、周榜新期次不受人工行影响、手动单仓重生不受限）

## 6. 接口接线

- [ ] 6.1 `app/web/routes.py`：`GET /api/local-ai/tasks`（`limit` 默认 20/上限 50、`kind` 过滤、`format=text|json`）
- [ ] 6.2 `app/web/routes.py`：`POST /api/local-ai/fill`（单批条目上限 200、请求体上限 1MB，超限整批拒绝）
- [ ] 6.3 契约测试（`tests/test_local_ai.py` 或 `tests/test_web*.py`）：响应形状、参数越界、错误码、鉴权沿用（不新增鉴权代码）

## 7. 文档与冻结物

- [ ] 7.1 `docs/dev-commands.md`：新增导出/回填 curl 用法（含 Basic Auth 形态与一个最小示例）
- [ ] 7.2 《交互流程说明》新增专章（本地生成回传流程）+ §8.4 写入口径回写"人工行受保护"（**待本人确认后写入**）
- [ ] 7.3 《任务拆解表》新增任务行并回填验收状态

## 8. 验证与收口

- [ ] 8.1 `tools/verify.ps1` 一次跑绿（ruff + pytest）
- [ ] 8.2 真实环境闭环预演（本地起服务 + 生产库副本）：导出 → 本地生成 → 回填 → 页面核对渲染（A 级）
- [ ] 8.3 部署（`docs/dev-commands.md` §4，`.env.prod` 保持 `AI_ENABLED=0`）+ 生产 `limit=3` 实跑闭环 + 回环冒烟
- [ ] 8.4 独立评审通过后归档 change 并同步 `openspec/specs/`
