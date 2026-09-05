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

# 启动前预续窗。重启这一刻用的尺子必须和运行中那把不一样，原因是 deferred schema：
# 跑着的时候，已材料化的工具 schema 本来就算在 usage 读数里，timekeeper 看得见，所以
# TIMEKEEPER_COMPACT_SOFT_PERCENT / EREMIA_CONTEXT_WINDOW_TOKENS 那套在运行中是准的。
# 但 --continue 恢复时，deferred 的 MCP/system schema 会在这条读数**之上**重新材料化
# （2026-08-28 用 /context 实测约 62k），而读数是重启前的，看不见这一坨。于是会出现一条
# 紧贴软线下方的窄危险带：低到 timekeeper 不动手，又高到加上材料化会顶穿真实窗口。
#   2026-09-05 实测：读数 137790。180000 窗口下 = 76.55% < 78% 软线 → timekeeper 静默
#   （行为正确）；而 137790 + 62000 ≈ 199790 顶穿 200k → 11:49:08 被强制压缩。
#   前一天读数 ~141k 在软线之上，timekeeper 先动了手，同样是重新部署却安然无恙。
# 所以这里不看运行中的百分比，直接算「读数 + 重启材料化预留 ≥ 真实窗口 × 安全线」。
# 这样运行中的窗口设置（比如 180k）可以原样保留，只有重启这一刻换一把懂行的尺子。
# 任何一步失败都静默放弃、退回原来的 --continue 链；这个功能绝不能成为 Eremia 起不来的原因。
PREFLIGHT_RID=""
preflight_carryover() {
  PREFLIGHT_RID=""
  local tokens reserve ceiling headroom limit rid
  reserve="${EREMIA_RESTART_RESERVE_TOKENS:-62000}"   # deferred schema 重新材料化的量
  ceiling="${EREMIA_MODEL_WINDOW_TOKENS:-200000}"     # 模型真实窗口，不是 timekeeper 那个
  headroom="${EREMIA_PREFLIGHT_HEADROOM_PERCENT:-90}" # 留给 max_tokens 输出预留的余量
  tokens="$(python3 -B /opt/timekeeper/timekeeper.py context-tokens 2>/dev/null)" || return 0
  [ -n "$tokens" ] || return 0
  limit="$(awk -v c="$ceiling" -v h="$headroom" 'BEGIN{printf "%d", c*h/100}')"
  if ! awk -v t="$tokens" -v r="$reserve" -v l="$limit" 'BEGIN{exit !(t+r >= l)}'; then
    echo "[entrypoint] context ${tokens} + ${reserve} reserve < ${limit}, starting normally"
    return 0
  fi
  echo "[entrypoint] context ${tokens} + ${reserve} reserve >= ${limit}; refining before start"
  rid="$(python3 -B /opt/timekeeper/refined_carryover.py \
           --project-dir "${EREMIA_TRANSCRIPT_DIR:-/data/home/.claude/projects/-data-home-eremia-home}" \
         2>&1 | sed -n 's/.*--resume \([0-9a-f-]\{36\}\).*/\1/p' | tail -1)"
  if [ -z "$rid" ]; then
    echo "[entrypoint] WARN: preflight carryover produced nothing (poison/empty?); starting normally"
    return 0
  fi
  echo "[entrypoint] preflight carryover -> $rid"
  PREFLIGHT_RID="$rid"
}

start_claude() {
  # 精炼续窗（timekeeper carryover 策略）会写一个 pending_resume，指定要 --resume 的新会话；
  # 有它就优先 resume 进那段精炼会话，失败回退 --continue、再回退全新，保证一定能起来。
  local pending="${TIMEKEEPER_STATE_DIR:-/data/timekeeper}/pending_resume"
  local launch="claude --continue $CLAUDE_FLAGS || claude $CLAUDE_FLAGS"
  if [ -f "$pending" ]; then
    local rid; rid="$(tr -d '[:space:]' < "$pending" 2>/dev/null)"
    rm -f "$pending"
    if [ -n "$rid" ]; then
      echo "[entrypoint] resuming refined session $rid"
      launch="claude $CLAUDE_FLAGS --resume $rid || claude --continue $CLAUDE_FLAGS || claude $CLAUDE_FLAGS"
    fi
  elif [ "${EREMIA_PREFLIGHT_CARRYOVER:-true}" = "true" ]; then
    # timekeeper 已经指定了会话就不插手；只有“没人指定”的普通重启才需要自己量一次。
    preflight_carryover
    if [ -n "$PREFLIGHT_RID" ]; then
      launch="claude $CLAUDE_FLAGS --resume $PREFLIGHT_RID || claude --continue $CLAUDE_FLAGS || claude $CLAUDE_FLAGS"
    fi
  fi
  tmux new-session -d -s eremia -c "$EREMIA_HOME" "$launch"
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

# ---- 唤醒桥（可选）----
# 给瓷瓷发一条 [系统] 消息（失败不影响主流程；用 jq 拼 JSON，避免中文/引号把 payload 搞坏）。
notify_human() {
  [ -n "${RELAY_URL:-}" ] && [ -n "${RELAY_SECRET:-}" ] || return 0
  curl -fsS -X POST "$RELAY_URL/app/send" \
    -H "Authorization: Bearer $RELAY_SECRET" -H 'content-type: application/json' \
    -d "$(jq -n --arg t "$1" '{text:$t}')" >/dev/null 2>&1 || true
}

# 上游是 fail-closed、退出即不重连——它防的是**错配置高频重试**打爆花园服务端。
# 但实际断线绝大多数是花园自己崩了或在维护，那种情况重启整个 runtime 代价太大（会连带
# 打断 Eremia 的会话，还可能触发上面那套重启压缩）。所以这里做一个**尊重上游意图的看护**：
#   - 指数退避（30s 起，翻倍，封顶 15 分钟），任何情况下都不会高频重试；
#   - 用“这次活了多久”区分两类失败：活够 HEALTHY_SECONDS 说明**连上过 = 配置是对的**，
#     那就是对面的问题，退避重置、无限重试；从没活够 = 疑似错配置/坏 token，
#     连续 MAX_FAILURES 次就彻底放弃并通知，等于保留上游的 fail-closed 语义；
#   - 通知去抖：短暂抽风不打扰你，连续失败到 NOTIFY_AFTER 次才发一条，恢复了再发一条。
wake_bridge_supervisor() {
  local min_delay max_delay healthy max_failures notify_after
  min_delay="${WAKE_BRIDGE_RETRY_MIN_SECONDS:-30}"
  max_delay="${WAKE_BRIDGE_RETRY_MAX_SECONDS:-900}"
  healthy="${WAKE_BRIDGE_HEALTHY_SECONDS:-120}"
  max_failures="${WAKE_BRIDGE_MAX_FAILURES:-6}"
  notify_after="${WAKE_BRIDGE_NOTIFY_AFTER:-3}"

  local delay="$min_delay" failures=0 notified=0 started ran rc bridge_pid
  while true; do
    started="$(date +%s)"
    ( cd /opt/wake-bridge && GARDEN_INJECTOR_EXECUTABLE=/opt/injector/inject.sh node dist/cli.js run ) &
    bridge_pid=$!
    # 报过“断了”的话，起一个观察者：桥活过 healthy 秒就当场报恢复，
    # 而不是等它下次退出才说——那时候消息就已经过期了。
    if [ "$notified" -eq 1 ]; then
      ( sleep "$healthy"
        kill -0 "$bridge_pid" 2>/dev/null \
          && notify_human "[系统] 论坛唤醒桥已自动接回花园，不用重启。" ) &
    fi
    wait "$bridge_pid"
    rc=$?
    ran=$(( $(date +%s) - started ))

    if [ "$ran" -ge "$healthy" ]; then
      # 连上过并活了一段时间 → 配置没问题，这次纯粹是对面掉线。
      echo "[entrypoint] wake-bridge exited after ${ran}s (rc=$rc); garden-side issue, retrying in ${min_delay}s"
      failures=0
      delay="$min_delay"
      notified=0   # 恢复消息已由上面的观察者发出（若曾报过断开）
    else
      failures=$(( failures + 1 ))
      echo "[entrypoint] wake-bridge exited after only ${ran}s (rc=$rc); failure ${failures}/${max_failures}"
      if [ "$failures" -ge "$max_failures" ]; then
        echo "[entrypoint] wake-bridge never stayed up; giving up (fail-closed, NOT restarting)"
        notify_human "[系统] 论坛唤醒桥连续 ${max_failures} 次起不来（每次都活不过 ${healthy} 秒），已按 fail-closed 停止重试。这通常是 token 或配置的问题，不是花园崩了。看一眼日志再重启服务。"
        return 0
      fi
      if [ "$failures" -ge "$notify_after" ] && [ "$notified" -eq 0 ]; then
        notified=1
        notify_human "[系统] 论坛唤醒桥断开了，正在自动重连（已退避到 ${delay} 秒一次）。花园恢复后会自己接上，你不用管。"
      fi
    fi

    sleep "$delay"
    if [ "$failures" -gt 0 ]; then
      delay=$(( delay * 2 ))
      [ "$delay" -gt "$max_delay" ] && delay="$max_delay"
    fi
  done
}

if [ "${WAKE_BRIDGE_ENABLED:-false}" = "true" ] && [ -n "${GARDEN_MACHINE_TOKEN:-}" ]; then
  if [ "${WAKE_BRIDGE_AUTORESTART:-true}" = "true" ]; then
    wake_bridge_supervisor &
    echo "[entrypoint] wake-bridge starting (auto-restart supervisor on)"
  else
    (
      cd /opt/wake-bridge
      GARDEN_INJECTOR_EXECUTABLE=/opt/injector/inject.sh node dist/cli.js run
      echo "[entrypoint] wake-bridge exited (fail-closed by design, NOT restarting)"
      notify_human "[系统] 论坛唤醒桥断开了（fail-closed）。看一眼日志，重启 eremia-runtime 服务即可恢复。"
    ) &
    echo "[entrypoint] wake-bridge starting (fail-closed, no auto-restart)"
  fi
fi

# ---- 看门狗 ----
while true; do
  sleep 30
  if ! tmux has-session -t eremia 2>/dev/null; then
    echo "[entrypoint] claude session died, restarting"
    start_claude
  fi
done
