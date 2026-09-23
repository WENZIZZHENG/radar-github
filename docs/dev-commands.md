# 开发命令手册

> 本项目日常开发与验证命令的唯一登记处，均已实测跑通（Windows + Git Bash 环境）。
> 新增或变更命令必须实际跑通后才写入本文件；统一收口验证入口见 §1，收口只认它一次跑绿。

## 1. 统一验证入口（ruff + pytest）

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1
```

## 2. 本地起服务

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

## 3. 依赖安装

```bash
uv venv .venv && uv pip install -r pyproject.toml --extra dev
```

> 踩坑记录（2026-08-20）：`&&` 连写是 bash 语法，**Windows PowerShell 5.1 不支持**（报"标记 && 不是此版本中的有效语句分隔符"）。
> PowerShell 下拆两行跑：`uv venv .venv` 然后 `uv pip install -r pyproject.toml --extra dev`；或直接用 Git Bash 跑原命令。
> 本手册所有 `&&` 连写同理（本地命令默认 Git Bash 环境）。

## 4. 生产部署（tar+scp，2026-08-14 实跑固化）

> 服务器：root@<SERVER_IP>（CentOS 9）；应用 /opt/radar 归 radar 用户；systemd 单元 radar.service；
> Caddy 反代 radar.example.com → 127.0.0.1:8000（Basic Auth）。**本地仓库有 origin remote（GitHub），但生产部署不走 git pull，走下方 tar+scp 流程。**

### 4.0 前置：SSH 私钥（本次踩坑点，必先核对）

- 私钥固定用 `~/.ssh/your-deploy-key.pem`（默认 id_rsa 服务器不认，会 Permission denied）。
- 以下命令里的 `$SSH` 指：`ssh -i ~/.ssh/your-deploy-key.pem -o BatchMode=yes root@<SERVER_IP>`
  （scp 同理加 `-i`）。Git Bash 下 ssh 会刷 post-quantum 警告，装饰性噪音，管道 `grep -v` 滤掉即可。
- 连通性自检：`$SSH 'hostname && systemctl is-active radar'` → 应回主机名 + `active`。

### 4.1 打包上传（本地 Git Bash，项目根）

```bash
tar czf /tmp/radar-deploy.tar.gz --exclude='./.git' --exclude='./.env' --exclude='./.env.dev' --exclude='./.env.prod' --exclude='./data' \
  --exclude='./.venv' --exclude='./.idea' --exclude='./.pytest_cache' --exclude='./.ruff_cache' \
  --exclude='./__pycache__' --exclude='*/__pycache__' .
sha256sum /tmp/radar-deploy.tar.gz   # 记一下，服务器侧要核对
scp -i ~/.ssh/your-deploy-key.pem /tmp/radar-deploy.tar.gz root@<SERVER_IP>:/tmp/
```

排除清单是红线：`.env*`（密钥不进包——文件名按环境错开：本地 `.env.dev`、生产 `.env.prod`，应用代码只读 `.env.dev`，生产由 radar.service 的 EnvironmentFile 注入，本地配置误传上去也不会被加载）、`data/`（生产库绝不能被本地库覆盖）。`.env.example` 模板正常入包。**`.env.prod` 例外口径（2026-08-20 本人拍板）：本地 `.env.prod` 是生产配置的唯一事实来源，每次部署必须单独 scp 覆盖生产（见 4.2 第 ② 步），防两端漂移。**

### 4.2 服务器侧解压＋重启

```bash
# ① 代码包：本地先 scp /tmp/radar-deploy.tar.gz，服务器侧核对 sha 后解压
$SSH '
set -e
sha256sum /tmp/radar-deploy.tar.gz        # 必须与本地一致，不一致中止
tar xzf /tmp/radar-deploy.tar.gz -C /opt/radar --no-same-owner
'
# ② .env.prod 覆盖（2026-08-20 本人拍板：每次部署必做，本地为唯一事实来源）
scp -i ~/.ssh/your-deploy-key.pem .env.prod root@<SERVER_IP>:/tmp/radar-env.prod
$SSH 'install -o radar -g radar -m 600 /tmp/radar-env.prod /opt/radar/.env.prod'
# ③ 权限＋重启＋活性检查
$SSH '
set -e
chown -R radar:radar /opt/radar
systemctl restart radar
sleep 4
systemctl is-active radar                 # 必须 active
'
```

依赖有变更（pyproject.toml 动了）时，重启前加一步（否则跳过）：
`sudo -u radar -H bash -c 'cd /opt/radar && export PATH="$HOME/.local/bin:$PATH" && uv pip install --python .venv/bin/python -r pyproject.toml'`
（PyPI 直连若超时，加 `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`，T-012 踩过。）

### 4.3 验证（三道）

```bash
# ① 回环冒烟：全 200 才合格
$SSH 'for u in "/" "/?board=all" "/total" "/total?board=all"; do
  echo "$u -> $(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000$u)"; done'
# ② 耗时对比（性能类改动做）：同命令把 %{http_code} 换 %{time_total}，部署前后各测 3 次留痕
# ③ 公网鉴权墙（本地跑）：curl -s -o /dev/null -w "%{http_code}" https://radar.example.com/ → 401 为正常
#    （对密码 200 只能本人实测：明文密码不落任何文件，AI 侧只有 hash 没法测）
```

### 4.4 本地 AI 生成回传（local-ai-relay，T-040）

生产 AI 段停用后，文本改由"本地工具生成 + 接口回填"。两个端点：导出 `GET /api/local-ai/tasks`、回填 `POST /api/local-ai/fill`；判定与写入语义见 `docs/sop/交互流程/20-本地AI生成回传.md`，接口契约见 `openspec/specs/local-ai-relay/`。

服务器回环形态（**仅服务器上可用**，免鉴权，排障/冒烟用；2026-09-13 预演实测）：

```bash
# 导出作业单（text 形态，整段粘贴给本地工具）：limit 默认 20、上限 50；kind 可 all|translate|week|quarter|total|summary
curl -s "http://127.0.0.1:8000/api/local-ai/tasks?limit=20&format=text" -o sheet.txt
# 导出结构化清单（脚本消费）：字段 as_of/week_label/quarter_label/window_open/remaining/probe_skipped/probe_truncated/items
curl -s "http://127.0.0.1:8000/api/local-ai/tasks?limit=20&format=json" -o tasks.json
# 回填（体可为 {"items":[...],"overwrite":false} 或裸数组；条目前后可有围栏，服务端会剥）
curl -s -X POST "http://127.0.0.1:8000/api/local-ai/fill" -H "Content-Type: application/json" --data-binary @result.json
```

公网形态（**本人在自己机器上用的形态**：`https://radar.example.com` ＋ Basic Auth；密码本人持有，AI 侧只有 hash 无法代跑）：

```bash
curl -s -u "<用户>:<密码>" "https://radar.example.com/api/local-ai/tasks?limit=20&format=text" -o sheet.txt
curl -s -u "<用户>:<密码>" -X POST "https://radar.example.com/api/local-ai/fill" -H "Content-Type: application/json" --data-binary @result.json
```

回填条目形状：`{"repo":"owner/name","kind":"week","period_label":"2026-W37","text":"正文"}`；`translate` 条目另须原样回带 `src`（作业单「回填字段」行给出，原文变更会被拒收）。错误码：`400`（JSON 非法/kind、format 非法/单批超 200 条/期次过期等按条计 errors）、`413`（请求体超 1MB）。响应形如 `{"written":n,"skipped":n,"failed":n,"errors":[{"repo":...,"kind":...,"reason":...}]}`。
提示：窗口日（每月 1/15 号）导出会按段拉 README 比对，单次实际约 200~400 次 GitHub 调用、可能耗时 1~3 分钟，**期间站点（榜单页面）短暂无响应属预期**；`probe_skipped>0` 只是"本轮还有候选未判定"的告警（探测无服务端状态，重复导出不会清零、不代表剩余工作量），继续"导出→回填"逐批推进即可；**窗口日的收工判据＝本次导出没有任何 total/概要重生任务**（非窗口日＝`remaining` 归零）。

### 4.5 收尾与回滚

- 表结构变更无需手工迁移：lifespan 的 `init_db()` 全量 IF NOT EXISTS 自动建表。
- 首次启用榜单预计算（T-027 类）可手跑一轮立即生效，不等次日 05:00：
  `$SSH "sudo -u radar -H bash -c 'cd /opt/radar && .venv/bin/python -c \"from app.db import get_conn; from app.classify import load_topics; from app.config import BASE_DIR; from app.report import precompute_boards; c=get_conn(); print(precompute_boards(c, load_topics(BASE_DIR/\\\"config\\\"/\\\"topics.yaml\\\"))); c.close()\"'"`
  （引号层数深，复杂脚本改走 `ssh 'bash -s' <<EOF` 管道更稳。）
- 回滚：无 git remote，回滚 = 本地 `git checkout <旧 commit>` 重打 tar 重走 4.1~4.3；生产 data/ 与 .env.prod 不受影响。
- 部署结果回填《任务拆解表》对应任务详情卡（含耗时对比留痕）。

### 4.6 数据快照发布（GitHub Release）

生产库快照作为 GitHub Release 附件发布，**不进 git 历史**（二进制文件会持续撑大仓库）。2026-09-23 实跑固化，tag `data-20260923`。前置：gh 已登录（身份 WENZIZZHENG），仓库远端 `WENZIZZHENG/radar-github`。

> 踩坑记录（2026-09-23）：① gh 装在 `"/c/Program Files/GitHub CLI/gh.exe"`，**不在 PATH，必须全路径调用**；② **所有 gh 命令必须带代理环境变量**，github.com 及 uploads 域名直连不通，走本机 `127.0.0.1:7890`；③ 换机器/首次使用需先 `gh auth login --web` 走设备码授权（本次未重跑，已登录状态）。

```bash
export HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890
GH="/c/Program Files/GitHub CLI/gh.exe"
D=$(date +%Y%m%d)          # 本次实测用实值 20260923；下列命令按当日日期替换 $D

# ① 打包（gzip -c 默认级别；-t 自检必须 OK；实测：解压后与 data/radar.db 逐字节一致）
gzip -c data/radar.db > /tmp/radar-$D.db.gz && gzip -t /tmp/radar-$D.db.gz

# ② 建 Release 并传附件（一条命令完成；--notes 里的统计值用当日库实值填，别照抄）
"$GH" release create data-$D /tmp/radar-$D.db.gz --repo WENZIZZHENG/radar-github \
  --title "数据快照 $D" \
  --notes "生产 SQLite 库当日快照（gzip）。内容：repos=…、star_snapshots=…、max(captured_at)=…。用法：gunzip 后作为 data/radar.db 即可被应用直接读取（WAL 模式，init_db 幂等建表/迁移）。"
# 输出即 release URL：https://github.com/WENZIZZHENG/radar-github/releases/tag/data-20260923

# ③ 查看（asset 行有附件即成功）＋下载核对（两侧 sha256 必须一致）
"$GH" release view data-$D --repo WENZIZZHENG/radar-github
"$GH" release download data-$D --repo WENZIZZHENG/radar-github -p 'radar-*.db.gz' -D /tmp/radar-dl
sha256sum /tmp/radar-$D.db.gz /tmp/radar-dl/radar-$D.db.gz
# ④ 附件链接可达性（302 → release-assets.githubusercontent.com，末段 200）
curl -sIL "https://github.com/WENZIZZHENG/radar-github/releases/download/data-$D/radar-$D.db.gz" | grep -Ei "^HTTP/|^location:"
```

备注：`gh release view` 实测输出含 `asset: radar-20260923.db.gz`；`gh release create` 一次成功、无重试，上传 9.3MB 约 2 秒；附件 sha256 与本地打包文件一致，解压后 `PRAGMA quick_check` 为 `ok`。

