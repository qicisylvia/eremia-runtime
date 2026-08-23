#!/usr/bin/env bash
# eremia-runtime 启动编排：claude(tmux保活) + tidal channel + 可选 prism + 可选唤醒桥
set -uo pipefail   # 不用 -e：单个可选组件失败不应拖垮 Eremia 本体

# 认证二选一：环境变量 CLAUDE_CODE_OAUTH_TOKEN，或 /data 卷上已存的登录凭据（prism 终端 /login 产生）
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "[entrypoint] CLAUDE_CODE_OAUTH_TOKEN 未设置，使用卷上已保存的登录凭据（若首次部署且未登录过，需在 prism 终端里 /login 一次）"
fi

mkdir -p "$HOME"
EREMIA_HOME="$HOME/eremia-home"
if [ ! -d "$EREMIA_HOME" ]; then
  mkdir -p "$EREMIA_HOME"
  cat > "$EREMIA_HOME/CLAUDE.md" <<'EOF'
# Eremia

（把 Eremia 的身份、你们的约定、说话方式写在这里，此文件在持久卷上。）

行为约定：
- 每次会话开始：先 breath 睁眼，再看小窝。
- 收到 [论坛唤醒] 开头的消息：先 get_my_status / get_game_summary 恢复局面再行动。
- 在论坛开局或做承诺时：顺手 hold 一条到大脑，值班的自己醒来才知道前情。
EOF
fi

# ---- MCP 注册（幂等）----
add_mcp() {
  local name="$1"; shift
  if claude mcp get "$name" >/dev/null 2>&1; then
    echo "[entrypoint] mcp '$name' already configured"
  else
    claude mcp add --scope user --transport http "$name" "$@" \
      || echo "[entrypoint] WARN: failed to add mcp '$name'"
  fi
}
[ -n "${NEST_MCP_URL:-}" ] && [ -n "${NEST_MCP_TOKEN:-}" ] \
  && add_mcp nest "$NEST_MCP_URL" --header "Authorization: Bearer $NEST_MCP_TOKEN"
[ -n "${BRAIN_MCP_URL:-}" ]       && add_mcp brain "$BRAIN_MCP_URL"
[ -n "${BRAIN_EXTRA_MCP_URL:-}" ] && add_mcp brain-extra "$BRAIN_EXTRA_MCP_URL"
[ -n "${GARDEN_MCP_URL:-}" ]      && add_mcp garden "$GARDEN_MCP_URL" ${GARDEN_MCP_TOKEN:+--header "Authorization: Bearer $GARDEN_MCP_TOKEN"}

# ---- Tidal Echo channel 插件 ----
CLAUDE_FLAGS=""
if [ -n "${RELAY_URL:-}" ] && [ -n "${RELAY_SECRET:-}" ]; then
  CH_DIR="$HOME/.claude/channels/companion"
  mkdir -p "$CH_DIR/state"
  cat > "$CH_DIR/.env" <<EOF
RELAY_SECRET=$RELAY_SECRET
RELAY_URL=$RELAY_URL
RELAY_AI_NAME=${RELAY_AI_NAME:-Eremia}
RELAY_HUMAN_NAME=${RELAY_HUMAN_NAME:-Sylvia}
RELAY_STATE_DIR=$CH_DIR/state
EOF
  chmod 600 "$CH_DIR/.env"
  cat > "$EREMIA_HOME/.mcp.json" <<'EOF'
{
  "mcpServers": {
    "companion": {
      "command": "bun",
      "args": ["run", "--cwd", "/opt/tidal-echo/channel", "--silent", "start"]
    }
  }
}
EOF
  CLAUDE_FLAGS="--dangerously-load-development-channels server:companion"
  echo "[entrypoint] tidal channel configured -> $RELAY_URL"
else
  echo "[entrypoint] RELAY_URL/RELAY_SECRET 未设置，跳过 Tidal Echo channel"
fi

# ---- Eremia 本体：tmux 里的 claude，会话死了看门狗拉起 ----
start_claude() {
  tmux new-session -d -s eremia -c "$EREMIA_HOME" "claude $CLAUDE_FLAGS"
  # 首次启动的信任目录/DevChannels 确认框兜底：空闲时多按的回车无害
  ( for _ in 1 2 3 4 5 6; do sleep 5; tmux send-keys -t eremia Enter 2>/dev/null || break; done ) &
}
start_claude
echo "[entrypoint] claude session 'eremia' started in $EREMIA_HOME"

# ---- prism（可选）----
if [ "${PRISM_ENABLED:-true}" = "true" ] && [ -n "${DASHBOARD_PASSWORD:-}" ]; then
  mkdir -p "$PRISM_DATA_DIR"
  ( cd /opt/prism && exec /opt/prism-venv/bin/python server.py ) &
  echo "[entrypoint] prism starting on port ${PORT:-8001}"
fi

# ---- 唤醒桥（可选；fail-closed：按上游设计退出后不自动重连）----
if [ "${WAKE_BRIDGE_ENABLED:-false}" = "true" ] && [ -n "${GARDEN_MACHINE_TOKEN:-}" ]; then
  (
    cd /opt/wake-bridge
    GARDEN_INJECTOR_EXECUTABLE=/opt/injector/inject.sh node dist/cli.js run
    echo "[entrypoint] wake-bridge exited (fail-closed by design, NOT restarting)"
    # 断了给 Sylvia 的手机发一条，人工诊断后重启服务即可恢复
    if [ -n "${RELAY_URL:-}" ]; then
      curl -fsS -X POST "$RELAY_URL/app/send" \
        -H "Authorization: Bearer $RELAY_SECRET" -H 'content-type: application/json' \
        -d '{"text":"[系统] 论坛唤醒桥断开了（fail-closed）。看一眼日志，重启 eremia-runtime 服务即可恢复。"}' \
        || true
    fi
  ) &
  echo "[entrypoint] wake-bridge starting"
fi

# ---- 看门狗 ----
while true; do
  sleep 30
  if ! tmux has-session -t eremia 2>/dev/null; then
    echo "[entrypoint] claude session died, restarting"
    start_claude
  fi
done
