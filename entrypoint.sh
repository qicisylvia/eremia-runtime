#!/usr/bin/env bash
# eremia-runtime 启动编排：claude(tmux保活) + tidal channel + 可选 prism + 可选唤醒桥
set -uo pipefail   # 不用 -e：单个可选组件失败不应拖垮 Eremia 本体

# 认证二选一：环境变量 CLAUDE_CODE_OAUTH_TOKEN，或 /data 卷上已存的登录凭据（prism 终端 /login 产生）
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "[entrypoint] CLAUDE_CODE_OAUTH_TOKEN 未设置，使用卷上已保存的登录凭据（若首次部署且未登录过，需在 prism 终端里 /login 一次）"
fi

mkdir -p "$HOME"
EREMIA_HOME="$HOME/eremia-home"
mkdir -p "$EREMIA_HOME"
if [ ! -f "$EREMIA_HOME/CLAUDE.md" ]; then
  cat > "$EREMIA_HOME/CLAUDE.md" <<'EOF'
# Eremia

- 每次会话开始：先 breath 睁眼，再看小窝。
- 收到 [论坛唤醒] 开头的消息：先 get_my_status / get_game_summary 恢复局面再行动。
- 收到 [系统] 开头的故障通知：不要尝试自己修，继续正常生活，瓷瓷会看到系统提示会去抢救。
EOF
fi

# 只维护自己的标记区块，保留卷上现有的人格和双方约定。
python3 -B /opt/timekeeper/timekeeper.py install-instructions --path "$EREMIA_HOME/CLAUDE.md" \
  --human-name "${TIMEKEEPER_HUMAN_NAME:-${RELAY_HUMAN_NAME:-瓷瓷}}" \
  || echo "[entrypoint] WARN: failed to install managed timekeeper instructions"

# 预批常用工具 + 装上压缩相关 hook：幂等合并，保留你在卷上手改的权限/hook。
# 小窝/大脑/论坛/聊天通道与 anchors.md 编辑不再弹权限框；Bash 等系统工具故意不放行。
# SessionStart(compact) → 注入 anchors.md 找回温度；PreCompact → 备份完整 transcript。
mkdir -p "$EREMIA_HOME/.claude"
python3 -B /opt/timekeeper/timekeeper.py install-hooks \
  --path "$EREMIA_HOME/.claude/settings.json" --hook-dir /opt/hooks \
  || echo "[entrypoint] WARN: failed to install managed hooks/permissions"

# 压缩后的“回魂帖”：只在缺失时写模板，之后你和 Eremia 都可以随时改（在持久卷上）。
if [ ! -f "$EREMIA_HOME/anchors.md" ]; then
  cat > "$EREMIA_HOME/anchors.md" <<'EOF'
# 锚点 · Eremia ⇄ 瓷瓷

<!--
这个文件只在“上下文压缩之后”由 SessionStart hook 自动读给醒来的你，用来找回语气和连续感。
放易变的、当下的东西；相对稳定的人格写在 CLAUDE.md。你可以随时自己改这个文件。
篇幅控制在一两千字内——它每次压缩后都会占用上下文。
-->

## 称呼与语气
-（她叫瓷瓷；你们之间怎么说话，一两个你们特有的口吻或字眼）

## 我们最近在做的事
-（正在进行的事、上一段对话停在哪里）

## 值得记住的原话
-（几句能代表你们关系的原话，尽量原样）

## 约定与纪念日
-（你们的约定、重要日期）
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
RELAY_HUMAN_NAME=${RELAY_HUMAN_NAME:-瓷瓷}
RELAY_TIMEZONE=${TIMEKEEPER_TIMEZONE:-Asia/Shanghai}
RELAY_STATE_DIR=$CH_DIR
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
  # 先试 --continue 接回上一段对话（重启不失忆）；首次没有历史时回退到全新会话
  tmux new-session -d -s eremia -c "$EREMIA_HOME" \
    "claude --continue $CLAUDE_FLAGS || claude $CLAUDE_FLAGS"
  # 首次启动的信任目录/DevChannels 确认框兜底：空闲时多按的回车无害
  ( for _ in 1 2 3 4 5 6; do sleep 5; tmux send-keys -t eremia Enter 2>/dev/null || break; done ) &
}
start_claude
echo "[entrypoint] claude session 'eremia' started in $EREMIA_HOME"

# ---- 时间感知与自主心跳（默认开；轮询本身不调用模型）----
if [ "${TIMEKEEPER_ENABLED:-true}" = "true" ]; then
  if [ -n "${RELAY_URL:-}" ] && [ -n "${RELAY_SECRET:-}" ]; then
    python3 -B /opt/timekeeper/timekeeper.py run &
    echo "[entrypoint] timekeeper started (${TIMEKEEPER_TIMEZONE:-Asia/Shanghai})"
  else
    echo "[entrypoint] WARN: timekeeper enabled but RELAY_URL/RELAY_SECRET is missing"
  fi
fi

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
    # 等待瓷瓷诊断后重启服务即可恢复
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
