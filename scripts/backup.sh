#!/usr/bin/env bash
# ==========================================================================
# Radar 雷达 · SQLite 每日备份脚本（T-013 起草物；在服务器执行，Windows 本地勿跑）
# 部署位置：/opt/radar/scripts/backup.sh（记得 chmod +x）
#
# cron（以 radar 用户 `crontab -e` 安装；每日 03:17 服务器本地时间跑本地备份，
# 周日由脚本内部额外推一份到异机；flock 防重入——上一次没跑完时新实例直接退出）：
#
#   # cron 环境干净，不读 .env / .bashrc，异机目标要在这里声明：
#   BACKUP_REMOTE=user@backup-host:/srv/radar-backups/
#   17 3 * * * flock -n /tmp/radar-backup.lock /opt/radar/scripts/backup.sh >> /opt/radar/backups/backup.log 2>&1
#
# 注意：cron 的日志重定向要求 /opt/radar/backups 已存在——
# 装 crontab 前先手动跑一遍本脚本（会自动建目录），或先 mkdir -p /opt/radar/backups。
#
# 恢复演练（T-013 验收动作；恢复到临时库验证可读，不碰在线库）：
#   gunzip -c /opt/radar/backups/daily/radar-<时间戳>.db.gz > /tmp/radar-restore.db
#   sqlite3 /tmp/radar-restore.db 'SELECT count(*) FROM repos;'
# 能查出仓库数即恢复有效，练完 rm 掉临时库。
#
# 环境变量：
#   RADAR_DB_PATH  SQLite 库路径，默认 /opt/radar/data/radar.db（与 deploy/radar.service 一致）
#   BACKUP_DIR     本地备份根目录，默认 /opt/radar/backups
#   BACKUP_REMOTE  异机目标，scp 格式 user@host:/path/dir/，仅每周日生效；
#                  未配置则跳过并提示。需事先配好免密登录（ssh-copy-id），
#                  否则 cron 里 scp 会卡在密码 prompt 直到超时。
# ==========================================================================
set -euo pipefail

DB_PATH="${RADAR_DB_PATH:-/opt/radar/data/radar.db}"
BACKUP_DIR="${BACKUP_DIR:-/opt/radar/backups}"
DAILY_DIR="${BACKUP_DIR}/daily"
KEEP=14   # 本地保留最近 14 份：约两周的回滚窗口，磁盘占用可控（库量级 MB）

TS="$(date +%Y%m%d-%H%M%S)"
TARGET="${DAILY_DIR}/radar-${TS}.db.gz"

mkdir -p "${DAILY_DIR}"

if [ ! -f "${DB_PATH}" ]; then
	echo "[backup] 库文件不存在：${DB_PATH}（可用 RADAR_DB_PATH 覆盖）" >&2
	exit 1
fi

# 热备用 sqlite3 .backup（SQLite 在线备份 API），而不是直接 cp——
# WAL 模式下最近写入可能还躺在 radar.db-wal 里未 checkpoint，直接 cp 主库文件
# 既会丢掉这部分数据，又可能拷到写入中途的不一致页；
# .backup 拿到的是一致性快照，且备份期间不阻塞应用正常读写。
# （mktemp 路径由本脚本生成、不含空格；若把 BACKUP_DIR 指到含空格的路径，
#   .backup 的参数需自行加引号）
TMP_DB="$(mktemp "${DAILY_DIR}/.radar-tmp-XXXXXX.db")"
trap 'rm -f "${TMP_DB}"' EXIT
sqlite3 "${DB_PATH}" ".backup ${TMP_DB}"

gzip -9 -c "${TMP_DB}" > "${TARGET}"
rm -f "${TMP_DB}"

# 轮转：文件名内嵌时间戳，字典序即时间序；只留最近 KEEP 份，更老的删掉
ls -1 "${DAILY_DIR}"/radar-*.db.gz | sort -r | tail -n "+$((KEEP + 1))" | xargs -r rm -f --

# 每周日（date +%u：周日=7）把最新一份推到异机——本地 + 异机双份，
# 防服务器整机故障时连备份一起丢。失败即非零退出，让 cron 日志里明确可见。
REMOTE_STATUS="非周日，跳过异机传输"
if [ "$(date +%u)" -eq 7 ]; then
	if [ -n "${BACKUP_REMOTE:-}" ]; then
		LATEST="$(ls -1 "${DAILY_DIR}"/radar-*.db.gz | sort -r | sed -n '1p')"  # sed 消费全部输入：pipefail 下防 head 早关管道 SIGPIPE 误伤（评审低-2）
		if scp -q "${LATEST}" "${BACKUP_REMOTE}"; then
			REMOTE_STATUS="已传输 ${LATEST} -> ${BACKUP_REMOTE}"
		else
			REMOTE_STATUS="异机传输失败（目标 ${BACKUP_REMOTE}），本地备份完好"
			echo "[backup] ${REMOTE_STATUS}" >&2
			exit 1
		fi
	else
		REMOTE_STATUS="周日但 BACKUP_REMOTE 未配置，跳过异机传输（建议尽早配置）"
		echo "[backup] ${REMOTE_STATUS}" >&2
	fi
fi

KEPT="$(ls -1 "${DAILY_DIR}"/radar-*.db.gz | wc -l)"
SIZE="$(du -h "${TARGET}" | cut -f1)"
echo "[backup] 完成：${TARGET}（${SIZE}），本地留存 ${KEPT} 份（上限 ${KEEP}）；${REMOTE_STATUS}"
