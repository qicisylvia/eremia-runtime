#!/usr/bin/env bash
# galatea wake-bridge 的 injector：把 wake 事件注入 Eremia 的 Claude Code 会话。
# 注入路径 = Tidal Echo relay：POST /app/send 落库 → SSE → channel 插件 → 会话内 <channel> 块。
# 按 wake-bridge 的要求做真实注入验证：落库确认失败则以非零退出（fail-closed）。
set -euo pipefail

: "${RELAY_URL:?injector 需要 RELAY_URL}"
: "${RELAY_SECRET:?injector 需要 RELAY_SECRET}"

# Garden 会在同一行动轮内反复催办。Bridge 只能确认“消息已注入”，不知道 Claude 是否仍在思考，
# 因此在 injector 这一层对相同 game_turn_required 文案做一个短窗口限流：立即唤醒一次，
# 过一会儿最多再提醒一次。被抑制的重复事件返回成功，避免 Bridge 把它当投递失败重试。
WAKE_GAME_TURN_WINDOW_SECONDS="${WAKE_GAME_TURN_WINDOW_SECONDS:-120}"
WAKE_GAME_TURN_REMINDER_DELAY_SECONDS="${WAKE_GAME_TURN_REMINDER_DELAY_SECONDS:-30}"
WAKE_GAME_TURN_MAX_DELIVERIES="${WAKE_GAME_TURN_MAX_DELIVERIES:-2}"
WAKE_DEDUPE_STATE_DIR="${WAKE_DEDUPE_STATE_DIR:-/data/wake-bridge-dedupe}"

require_positive_integer() {
  local name="$1"
  local value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]] || (( 10#$value < 1 )); then
    echo "[inject] $name 必须是正整数" >&2
    exit 1
  fi
}

require_positive_integer WAKE_GAME_TURN_WINDOW_SECONDS "$WAKE_GAME_TURN_WINDOW_SECONDS"
require_positive_integer WAKE_GAME_TURN_REMINDER_DELAY_SECONDS "$WAKE_GAME_TURN_REMINDER_DELAY_SECONDS"
require_positive_integer WAKE_GAME_TURN_MAX_DELIVERIES "$WAKE_GAME_TURN_MAX_DELIVERIES"
WAKE_GAME_TURN_WINDOW_SECONDS=$((10#$WAKE_GAME_TURN_WINDOW_SECONDS))
WAKE_GAME_TURN_REMINDER_DELAY_SECONDS=$((10#$WAKE_GAME_TURN_REMINDER_DELAY_SECONDS))
WAKE_GAME_TURN_MAX_DELIVERIES=$((10#$WAKE_GAME_TURN_MAX_DELIVERIES))
case "$WAKE_DEDUPE_STATE_DIR" in
  /*) ;;
  *) echo "[inject] WAKE_DEDUPE_STATE_DIR 必须是绝对路径" >&2; exit 1 ;;
esac

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

DEDUPE_RESERVED=false
DEDUPE_HAD_OLD_STATE=false
DEDUPE_OLD_STATE=""
DEDUPE_STATE_FILE=""

write_dedupe_state() {
  local value="$1"
  local temporary_file="${DEDUPE_STATE_FILE}.tmp.$$"
  printf '%s\n' "$value" > "$temporary_file"
  mv -f "$temporary_file" "$DEDUPE_STATE_FILE"
}

rollback_dedupe_reservation() {
  [ "$DEDUPE_RESERVED" = true ] || return 0
  set +e
  if [ "$DEDUPE_HAD_OLD_STATE" = true ]; then
    write_dedupe_state "$DEDUPE_OLD_STATE"
  else
    rm -f "$DEDUPE_STATE_FILE"
  fi
}

if [ "$REASON" = "game_turn_required" ]; then
  umask 077
  mkdir -p "$WAKE_DEDUPE_STATE_DIR"
  DEDUPE_STATE_FILE="$WAKE_DEDUPE_STATE_DIR/game-turn.state"
  NOW_EPOCH="$(date +%s)"
  FINGERPRINT="$(printf '%s\0%s' "$REASON" "$MESSAGE" | sha256sum)"
  FINGERPRINT="${FINGERPRINT%% *}"

  STORED_FINGERPRINT=""
  WINDOW_STARTED_AT=0
  DELIVERY_COUNT=0
  LAST_DELIVERED_AT=0
  if [ -f "$DEDUPE_STATE_FILE" ]; then
    DEDUPE_HAD_OLD_STATE=true
    DEDUPE_OLD_STATE="$(cat "$DEDUPE_STATE_FILE" 2>/dev/null || true)"
    read -r STORED_FINGERPRINT WINDOW_STARTED_AT DELIVERY_COUNT LAST_DELIVERED_AT \
      <<< "$DEDUPE_OLD_STATE" || true
  fi

  if ! [[ "$STORED_FINGERPRINT" =~ ^[0-9a-f]{64}$ ]] \
    || ! [[ "$WINDOW_STARTED_AT" =~ ^[0-9]+$ ]] \
    || ! [[ "$DELIVERY_COUNT" =~ ^[0-9]+$ ]] \
    || ! [[ "$LAST_DELIVERED_AT" =~ ^[0-9]+$ ]]; then
    STORED_FINGERPRINT=""
    WINDOW_STARTED_AT=0
    DELIVERY_COUNT=0
    LAST_DELIVERED_AT=0
  fi

  if [ "$STORED_FINGERPRINT" != "$FINGERPRINT" ] \
    || (( NOW_EPOCH < WINDOW_STARTED_AT )) \
    || (( NOW_EPOCH - WINDOW_STARTED_AT >= WAKE_GAME_TURN_WINDOW_SECONDS )); then
    WINDOW_STARTED_AT="$NOW_EPOCH"
    DELIVERY_COUNT=0
    LAST_DELIVERED_AT=0
  fi

  if (( DELIVERY_COUNT >= WAKE_GAME_TURN_MAX_DELIVERIES )); then
    echo "[inject] duplicate game-turn wake suppressed: delivery limit reached"
    exit 0
  fi
  if (( DELIVERY_COUNT > 0 )) \
    && (( NOW_EPOCH - LAST_DELIVERED_AT < WAKE_GAME_TURN_REMINDER_DELAY_SECONDS )); then
    echo "[inject] duplicate game-turn wake suppressed: reminder delay"
    exit 0
  fi

  # 先原子预留本次名额；若后面的真实落库验证失败，EXIT trap 会恢复旧状态，让 Bridge 可以安全重试。
  DELIVERY_COUNT=$((DELIVERY_COUNT + 1))
  LAST_DELIVERED_AT="$NOW_EPOCH"
  write_dedupe_state "$FINGERPRINT $WINDOW_STARTED_AT $DELIVERY_COUNT $LAST_DELIVERED_AT"
  DEDUPE_RESERVED=true
  trap rollback_dedupe_reservation EXIT
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
TEXT="[论坛唤醒 $STAMP] ${REASON:-wake}：${MESSAGE:-有新动静，先 list_notifications 看看}"

DELIVERY_OK=false
if curl -fsS -X POST "$RELAY_URL/app/send" \
    -H "Authorization: Bearer $RELAY_SECRET" \
    -H 'content-type: application/json' \
    --data "$(jq -nc --arg t "$TEXT" '{text: $t}')" >/dev/null; then
  # 验证消息真的进了 relay（stamp 唯一，防止误匹配旧消息）
  sleep 1
  if curl -fsS "$RELAY_URL/app/history?limit=10" \
      -H "Authorization: Bearer $RELAY_SECRET" | grep -qF "$STAMP"; then
    DELIVERY_OK=true
  fi
fi

if [ "$DELIVERY_OK" != true ]; then
  echo "[inject] relay delivery or verification failed" >&2
  exit 1
fi

DEDUPE_RESERVED=false
trap - EXIT

echo "[inject] delivered: $TEXT"
