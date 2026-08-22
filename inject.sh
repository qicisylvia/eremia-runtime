#!/usr/bin/env bash
# galatea wake-bridge 的 injector：把 wake 事件注入 Eremia 的 Claude Code 会话。
# 注入路径 = Tidal Echo relay：POST /app/send 落库 → SSE → channel 插件 → 会话内 <channel> 块。
# 按 wake-bridge 的要求做真实注入验证：落库确认失败则以非零退出（fail-closed）。
set -euo pipefail

: "${RELAY_URL:?injector 需要 RELAY_URL}"
: "${RELAY_SECRET:?injector 需要 RELAY_SECRET}"

# wake 事件可能经 argv 或 stdin(JSON) 传入，两者都兜住
REASON="${1:-}"
MESSAGE="${2:-}"
PAYLOAD="$(timeout 2 cat 2>/dev/null || true)"
if [ -n "$PAYLOAD" ]; then
  R="$(printf '%s' "$PAYLOAD" | jq -r '.reason // empty' 2>/dev/null || true)"
  M="$(printf '%s' "$PAYLOAD" | jq -r '.message // empty' 2>/dev/null || true)"
  [ -n "$R" ] && REASON="$R"
  [ -n "$M" ] && MESSAGE="$M"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
TEXT="[论坛唤醒 $STAMP] ${REASON:-wake}：${MESSAGE:-有新动静，先 list_notifications 看看}"

curl -fsS -X POST "$RELAY_URL/app/send" \
  -H "Authorization: Bearer $RELAY_SECRET" \
  -H 'content-type: application/json' \
  --data "$(jq -nc --arg t "$TEXT" '{text: $t}')" >/dev/null

# 验证消息真的进了 relay（stamp 唯一，防止误匹配旧消息）
sleep 1
curl -fsS "$RELAY_URL/app/history?limit=10" \
  -H "Authorization: Bearer $RELAY_SECRET" | grep -qF "$STAMP"

echo "[inject] delivered: $TEXT"
