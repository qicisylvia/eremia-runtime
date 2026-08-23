#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data
cd /data   # 工作目录 = 持久卷，relay.db 落在这里

# 上游未固定 ASGI 模块名，默认 app:app，若不同用 RELAY_APP_MODULE 覆盖（如 server:app）
MODULE="${RELAY_APP_MODULE:-app:app}"
python -m uvicorn "$MODULE" \
  --app-dir /opt/tidal-echo/backend \
  --host 127.0.0.1 --port 3011 &

# 前端已在镜像构建时由仓库内 eremia-web 覆盖层完成定制。
exec nginx -g 'daemon off;'
