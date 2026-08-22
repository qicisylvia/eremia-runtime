#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data
cd /data   # 工作目录 = 持久卷，relay.db 落在这里

# 上游未固定 ASGI 模块名，默认 app:app，若不同用 RELAY_APP_MODULE 覆盖（如 server:app）
MODULE="${RELAY_APP_MODULE:-app:app}"
python -m uvicorn "$MODULE" \
  --app-dir /opt/tidal-echo/backend \
  --host 127.0.0.1 --port 3011 &

# 前端 CONFIG 定制（名字等）：直接改 /opt/tidal-echo/web/index.html 顶部，
# 或 fork 仓库改好后把 Dockerfile 里的 clone 地址换成你的 fork。
exec nginx -g 'daemon off;'
