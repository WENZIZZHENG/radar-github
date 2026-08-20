# GitHub 热门项目雷达（radar-github）

全自动 GitHub 热门项目雷达（单人自用）：每日采集跟踪池星数快照，产出分语言/分主题的周增量榜、季度回顾与总星榜，用于扩展技术知识面、支撑项目选型参考。

线上地址：`https://radar.example.com`（Caddy 反代 + Basic Auth 挡外人，无应用内用户体系）。

## 功能

- **每日采集**（北京 05:00 / UTC 21:00，进程内 APScheduler）：星数快照 + 发现池扩列 + 元数据漂移更新。
- **榜单**：周增量榜 / 季度回顾（90 天）/ 总星榜三口径，各 17 张分类榜（6 语言＋其它语言、9 主题＋其他）；增量 = 两端快照差，滑动窗口取数，首周缺席不上榜；新入池项目进"新崛起区"（入池增量 Top 10 分区，不与主榜混排）。
- **榜单预计算**（T-027 起）：每日采集后三口径 17 榜落 `board_cache` 表，页面打开直读；缓存缺失/过期自动降级实时算，不白屏；历史期次回看永远实时算。
- **关注与标签**：关注仓库独立页（按语言分组、当周增量）；自定义标签增删与筛选页。
- **AI 增强**（DeepSeek，全部可降级）：中文翻译（懒写入+批量补齐）、三维度推荐理由（周/季/总星）、AI 概要（README 文档视角）。AI 失败不阻塞出榜。

## 技术栈

FastAPI（SSR + Jinja2）· SQLite（单文件 + WAL）· APScheduler（进程内调度，无独立 worker）· httpx（GitHub GraphQL / DeepSeek）· 前端为少量原生 JS/CSS（无构建链）。Python ≥ 3.12。

## 目录结构

```
app/
  collector/    # GitHub 客户端、每日快照+发现
  web/          # 路由、模板、静态资源
  report.py     # 榜单计算（口径唯一实现）＋ 榜单预计算缓存读写
  jobs.py       # 进程内调度（每日任务：采集→预计算→AI）
  ai.py         # DeepSeek：翻译 / 推荐语 / 概要
  db.py + schema.sql  # 连接层与建表（全量 IF NOT EXISTS，启动自动建表）
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

## 文档与协作约定

产品层所有决策以 `docs/sop/` 为准（需求清单 / 功能闭环清单 / 交互流程说明 / 架构决策记录 / 任务拆解表 / 验收记录）；工程约定见根目录 `AGENTS.md`。数据硬约定：时间戳一律 UTC 定长 ISO（`YYYY-MM-DDTHH:MM:SSZ`），字典序即时间序。
