#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data
cd /data   # 工作目录 = 持久卷，relay.db 落在这里

# Web Push 密钥：首次启动自动生成在持久卷上，公钥打进日志
# （复制日志里的 Application Server Key 填进 VAPID_PUBLIC_KEY 环境变量后重启即生效）
if [ ! -f /data/private_key.pem ]; then
  vapid --gen >/dev/null 2>&1 || true
fi
if [ -f /data/private_key.pem ]; then
  echo "==================================================================="
  echo "[vapid] 把下面这串 Application Server Key 填进 VAPID_PUBLIC_KEY 环境变量"
  vapid --applicationServerKey || true
  echo "==================================================================="
fi

# 上游未固定 ASGI 模块名，默认 app:app，若不同用 RELAY_APP_MODULE 覆盖（如 server:app）
MODULE="${RELAY_APP_MODULE:-app:app}"
python -m uvicorn "$MODULE" \
  --app-dir /opt/tidal-echo/backend \
  --host 127.0.0.1 --port 3011 &

# 前端已在镜像构建时由仓库内 eremia-web 覆盖层完成定制。
exec nginx -g 'daemon off;'
