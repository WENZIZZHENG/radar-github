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

## 4. 生产部署（tar+scp，2026-08-14 实跑固化）

> 服务器：root@<SERVER_IP>（CentOS 9）；应用 /opt/radar 归 radar 用户；systemd 单元 radar.service；
> Caddy 反代 radar.example.com → 127.0.0.1:8000（Basic Auth）。**没有 git remote，部署不走 git pull。**

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

排除清单是红线：`.env*`（密钥只在服务器；文件名按环境错开——本地 `.env.dev`、生产 `.env.prod`，应用代码只读 `.env.dev`，生产由 radar.service 的 EnvironmentFile 注入，本地配置误传上去也不会被加载）、`data/`（生产库绝不能被本地库覆盖）。`.env.example` 模板正常入包。

### 4.2 服务器侧解压＋重启

```bash
$SSH '
set -e
sha256sum /tmp/radar-deploy.tar.gz        # 必须与本地一致，不一致中止
tar xzf /tmp/radar-deploy.tar.gz -C /opt/radar --no-same-owner
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

### 4.4 收尾与回滚

- 表结构变更无需手工迁移：lifespan 的 `init_db()` 全量 IF NOT EXISTS 自动建表。
- 首次启用榜单预计算（T-027 类）可手跑一轮立即生效，不等次日 05:00：
  `$SSH "sudo -u radar -H bash -c 'cd /opt/radar && .venv/bin/python -c \"from app.db import get_conn; from app.classify import load_topics; from app.config import BASE_DIR; from app.report import precompute_boards; c=get_conn(); print(precompute_boards(c, load_topics(BASE_DIR/\\\"config\\\"/\\\"topics.yaml\\\"))); c.close()\"'"`
  （引号层数深，复杂脚本改走 `ssh 'bash -s' <<EOF` 管道更稳。）
- 回滚：无 git remote，回滚 = 本地 `git checkout <旧 commit>` 重打 tar 重走 4.1~4.3；生产 data/ 与 .env.prod 不受影响。
- 部署结果回填《任务拆解表》对应任务详情卡（含耗时对比留痕）。

