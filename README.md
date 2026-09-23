# GitHub 热门项目雷达（radar-github）

全自动 GitHub 热门项目雷达（单人自用）：每日采集跟踪池星数快照，产出分语言/分主题的周增量榜、季度回顾与总星榜，用于扩展技术知识面、支撑项目选型参考。

线上地址：`https://radar.example.com`（Caddy 反代 + Basic Auth 挡外人，无应用内用户体系）。

## 功能

- **每日采集**（北京 05:00 / UTC 21:00，进程内 APScheduler）：星数快照 + 发现池扩列 + 元数据漂移更新。
- **榜单**：周增量榜 / 季度回顾（90 天）/ 总星榜三口径，各 17 张分类榜（6 语言＋其它语言、9 主题＋其他）；增量 = 两端快照差，滑动窗口取数，首周缺席不上榜；新入池项目进"新崛起区"（入池增量 Top 10 分区，不与主榜混排）。
- **榜单预计算**（T-027 起）：每日采集后三口径 17 榜落 `board_cache` 表，页面打开直读；缓存缺失/过期自动降级实时算，不白屏；历史期次回看永远实时算。
- **关注与标签**：关注仓库独立页（按语言分组、当周增量）；自定义标签增删与筛选页。
- **AI 增强**（DeepSeek，全部可降级）：中文翻译（懒写入+批量补齐）、三维度推荐理由（周/季/总星）、AI 概要（README 文档视角）。AI 失败不阻塞出榜。**生产现已停用自动 AI 段**（`.env.prod` 的 `AI_ENABLED=0`），文本改走"本地 AI 生成回传"通道——见下节。

## 技术栈

FastAPI（SSR + Jinja2）· SQLite（单文件 + WAL）· APScheduler（进程内调度，无独立 worker）· httpx（GitHub GraphQL / DeepSeek）· 前端为少量原生 JS/CSS（无构建链）。Python ≥ 3.12。

## 目录结构

```
app/
  collector/    # GitHub 客户端、每日快照+发现
  web/          # 路由、模板、静态资源
  report.py     # 榜单计算（口径唯一实现）＋ 榜单预计算缓存读写
  jobs.py       # 进程内调度（每日任务：采集→预计算→AI）
  ai.py         # AI 客户端与 prompt 构造（翻译 / 推荐语 / 概要；prompt 纯函数供导出复用）
  local_ai.py   # 本地 AI 生成回传：导出判定（与每日 ensure 同口径）/ 作业单 / 回填写入
  db.py + schema.sql  # 连接层与建表（全量 IF NOT EXISTS，启动自动建表+迁移）
config/topics.yaml    # 主题词表（封板物）
deploy/         # Caddyfile + systemd unit（生产配置起草物）
scripts/backup.sh     # 每日备份（cron 03:17，轮转 14 份）
docs/sop/       # 产品层决策唯一事实来源（需求/流程/架构决策/任务表/验收记录）
docs/dev-commands.md  # 日常开发与验证命令唯一登记处（含 §4 生产部署手册）
```

## 本地开发

```bash
uv venv .venv && uv pip install -r pyproject.toml --extra dev   # 装依赖
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000    # 起服务（Windows）
```

统一验证入口（收口只认它一次跑绿：ruff + pytest）：

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1
```

环境变量：git 只维护 `.env.example` 一套模板——本地复制为 `.env.dev`（应用代码只读它），生产复制为 `.env.prod`（放服务器 `/opt/radar/`，由 `radar.service` 的 EnvironmentFile 注入；两文件都不落仓）。模板默认姿态＝本地安全（`AI_ENABLED=0`、`RADAR_JOBS_ENABLED=0` 已写成实值），**生产实例化时删掉这两行 =0**。键位：`GITHUB_TOKEN`、`AI_API_KEY`（AI 提供方 key，兼容回退旧键 `DEEPSEEK_API_KEY`；缺失时 AI 功能降级、不阻塞出榜）、`AI_BASE_URL`/`AI_MODEL`（换 OpenAI 兼容提供方时改，缺省 DeepSeek）、`RADAR_DB_PATH`（缺省 `data/radar.db`）；逐键说明见 `.env.example` 注释。

## 生产部署

无 git remote，走 tar+scp 覆盖 `/opt/radar` + `systemctl restart radar`，完整手册（含 SSH 私钥路径、排除清单红线、验证三道、回滚）见 `docs/dev-commands.md` §4。

## 本地 AI 生成回传（local-ai-relay，T-040）

生产 AI 段已停用（`.env.prod` 的 `AI_ENABLED=0`）：文本不再由服务自动调模型生成，改由**本地对话式工具（有无 API 均可）按作业单生成、经接口回填**。判定口径与每日 ensure 同源（同一套范围集判定与 prompt 构造），只把"调用远端模型"换成"导出 → 本地生成 → 回填"。

两个端点（复用 Caddy Basic Auth，无匿名入口、不依赖 AI client）：

| 端点 | 作用 | 参数 |
|---|---|---|
| `GET /api/local-ai/tasks` | 导出待生成任务。`format=text`（缺省）出**自包含作业单**：输出格式要求 ＋ 逐条 system/user prompt 全文（README 已按 `AI_README_HEAD_CHARS` 截断）；`format=json` 出结构化清单（含 `as_of`/`week_label`/`remaining`/`probe_skipped`/`probe_truncated`） | `limit`（默认 20、上限 50）、`kind`（`all`/`translate`/`week`/`quarter`/`total`/`summary`）、`format`（`text`/`json`） |
| `POST /api/local-ai/fill` | 回填本地工具产出：默认**只补缺失**，`overwrite=true` 才覆盖；写入行标记 `source='manual'` | body 形如 `{"items":[{"repo":"o/n","kind":"week","period_label":"2026-W37","text":"…"}],"overwrite":false}` 或裸数组（可带 markdown 围栏）；`translate` 条目另须原样回带 `src` |

使用流程（四步；**在自己机器上用公网地址＋Basic Auth**——应用只绑回环 `127.0.0.1:8000`，公网唯一入口是 Caddy；在服务器上调试时才用回环形态免鉴权）：

1. 导出作业单（整段贴给本地工具）：
   `curl -s -u "<用户>:<密码>" "https://radar.example.com/api/local-ai/tasks?limit=20&format=text" -o sheet.txt`
2. 让本地工具只输出一个 JSON 对象：`{"items":[{"repo":…,"kind":…,"period_label":…,"text":…}]}`
3. 回填：
   `curl -s -u "<用户>:<密码>" -X POST "https://radar.example.com/api/local-ai/fill" -H "Content-Type: application/json" --data-binary @result.json`
4. 重复 1~3：**非窗口日**以 `remaining` 归零收工；**窗口日**（每月 1/15 号）以"本次导出没有任何 total/概要重生任务"收工——`probe_skipped>0` 只是"本轮还有候选没判定"的告警（探测无服务端状态，重复导出不会清零，不代表剩余工作量）。

口径与边界（细节见 `docs/sop/交互流程/20-本地AI生成回传.md`）：

- 范围照旧：S ＝ 三口径榜去重 ∪ 关注集；周榜维持每进新周全量重生（新期次新行）；半月窗口（1/15 号）对 total/概要按 README 指纹决定重生。
- 期次绑定：`week`/`quarter` 回填期次必须与当期一致，过期条目进 `errors`（重新导出即可）。
- `translate` 条目必须原样回带 8 位原文指纹 `src`（等价自动路径写译文前的 `description_en` 守卫，防过期译文入库）。
- **人工行保护**：`source='manual'` 的行不被任何自动路径覆盖（窗口日重生遇其直接跳过）；手动单仓"重新生成"仍可覆盖（写回 `ai`）。仓库简介在 GitHub 变更时，采集层照旧清译文并删除该仓全部文本（含人工行——输入变了文本即失效，下次导出会重新出现为缺失任务）。
- 响应与错误：`{"written","skipped","failed","errors"}`；`400`（JSON/参数非法、单批超 200 条）、`413`（请求体超 1MB）；逐条失败不阻断同批。
- 命令与公网形态（含 Basic Auth）见 `docs/dev-commands.md` §4.4；技术契约见 `openspec/specs/local-ai-relay/`（当前在 `openspec/changes/local-ai-relay/`，验收后归档）。

## 文档与协作约定

产品层所有决策以 `docs/sop/` 为准（需求清单 / 功能闭环清单 / 交互流程说明 / 架构决策记录 / 任务拆解表 / 验收记录）；工程约定见根目录 `AGENTS.md`。数据硬约定：时间戳一律 UTC 定长 ISO（`YYYY-MM-DDTHH:MM:SSZ`），字典序即时间序。
