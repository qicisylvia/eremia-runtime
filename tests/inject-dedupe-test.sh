#!/usr/bin/env bash
set -euo pipefail

INJECTOR="${1:?usage: inject-dedupe-test.sh /absolute/path/to/inject.sh}"
TEST_ROOT="$(mktemp -d)"
case "$TEST_ROOT" in
  /tmp/*) ;;
  *) echo "unexpected test directory: $TEST_ROOT" >&2; exit 1 ;;
esac

cleanup() {
  rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

FAKE_BIN="$TEST_ROOT/bin"
mkdir -p "$FAKE_BIN" "$TEST_ROOT/state"

cat > "$FAKE_BIN/jq" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-nc" ]; then
  shift
  text=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --arg)
        [ "${2:-}" = "t" ] && text="${3:-}"
        shift 3
        ;;
      *) shift ;;
    esac
  done
  printf '{"text":"%s"}\n' "$text"
else
  cat >/dev/null
fi
EOF

cat > "$FAKE_BIN/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
method="GET"
data=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -X) method="${2:-}"; shift 2 ;;
    --data) data="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done
if [ "$method" = "POST" ]; then
  [ ! -f "$FAKE_FAIL_FILE" ] || exit 22
  printf '%s\n' "$data" > "$FAKE_HISTORY_FILE"
  printf 'delivery\n' >> "$FAKE_DELIVERY_LOG"
else
  # FAKE_HIDE_HISTORY_FILE 模拟"消息确实送到了，但爆发时被挤出了 history 窗口"。
  # 注意旧版这里没有这个开关，GET 永远能查到刚 POST 的内容，所以验证永远成功——
  # 这就是 limit=10 那个真实 bug 从未被测出来的原因。
  [ ! -f "$FAKE_HIDE_HISTORY_FILE" ] || exit 0
  cat "$FAKE_HISTORY_FILE" 2>/dev/null || true
fi
EOF

cat > "$FAKE_BIN/date" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "+%s" ]; then
  cat "$FAKE_NOW_FILE"
else
  printf '20260824T000000Z\n'
fi
EOF

cat > "$FAKE_BIN/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

chmod +x "$FAKE_BIN/jq" "$FAKE_BIN/curl" "$FAKE_BIN/date" "$FAKE_BIN/sleep"

export PATH="$FAKE_BIN:$PATH"
export RELAY_URL="http://relay.test/relay"
export RELAY_SECRET="test-secret"
export WAKE_DEDUPE_STATE_DIR="$TEST_ROOT/state"
export WAKE_GAME_TURN_WINDOW_SECONDS=120
export WAKE_GAME_TURN_REMINDER_DELAY_SECONDS=30
export WAKE_GAME_TURN_MAX_DELIVERIES=2
export FAKE_NOW_FILE="$TEST_ROOT/now"
export FAKE_HISTORY_FILE="$TEST_ROOT/history"
export FAKE_DELIVERY_LOG="$TEST_ROOT/deliveries"
export FAKE_FAIL_FILE="$TEST_ROOT/fail-post"
export FAKE_HIDE_HISTORY_FILE="$TEST_ROOT/hide-history"

set_now() {
  printf '%s\n' "$1" > "$FAKE_NOW_FILE"
}

invoke_game() {
  "$INJECTOR" game_turn_required "${1:-游戏轮到你了。}" </dev/null
}

delivery_count() {
  if [ -f "$FAKE_DELIVERY_LOG" ]; then
    wc -l < "$FAKE_DELIVERY_LOG" | tr -d '[:space:]'
  else
    printf '0\n'
  fi
}

assert_deliveries() {
  local expected="$1"
  local actual
  actual="$(delivery_count)"
  if [ "$actual" != "$expected" ]; then
    echo "expected $expected deliveries, got $actual" >&2
    exit 1
  fi
}

# 同一文案在 90 秒内连续 8 次：第 1 次立即送达，第 4 次在 30 秒后成为唯一一次提醒。
for timestamp in 1000 1010 1020 1031 1040 1050 1060 1090; do
  set_now "$timestamp"
  invoke_game
done
assert_deliveries 2

# 120 秒窗口结束后，相同文案也视为新一轮并重新放行。
set_now 1120
invoke_game
assert_deliveries 3

# 文案变化视为新的行动；即使仍在原窗口内，也应立即送达。
set_now 1121
invoke_game "新的狼人杀行动。"
assert_deliveries 4

# 真正投递失败时必须恢复去重名额，下一次 Bridge 重试仍能送达。
set_now 1122
touch "$FAKE_FAIL_FILE"
if invoke_game "需要重试的行动。"; then
  echo "expected failed relay delivery" >&2
  exit 1
fi
rm -f "$FAKE_FAIL_FILE"
set_now 1123
invoke_game "需要重试的行动。"
assert_deliveries 5

# 非游戏轮次通知不参与该限流。
set_now 1124
"$INJECTOR" forum_notification_available "论坛通知 A" </dev/null
set_now 1125
"$INJECTOR" forum_notification_available "论坛通知 A" </dev/null
assert_deliveries 7

# 回归用例：POST 成功、但历史里查不到（爆发时被挤出 limit 窗口）。
# 2026-08-24 的七八条轰炸就是这么来的：旧代码把"查不到"当成"没送到"，回滚了去重名额，
# Bridge 按 maxAttempts=2 立刻重投 → 每次催办变成两条；而回滚清掉计数，
# 又让「120 秒内最多 2 条」永远撞不到上限，于是四次催办 = 八条。
# 正确行为：POST 成功就保留名额，非零退出照旧（fail-closed），但重投必须被限流器挡掉。
set_now 2000
touch "$FAKE_HIDE_HISTORY_FILE"
BEFORE="$(delivery_count)"
if invoke_game "被挤出窗口的行动。"; then
  echo "expected non-zero exit when history confirmation fails" >&2
  exit 1
fi
assert_deliveries "$((BEFORE + 1))"

# Bridge 的第二次投递（retryDelayMs=500，仍在同一秒）：必须被抑制，不能再送一条。
set_now 2000
if ! invoke_game "被挤出窗口的行动。"; then
  echo "expected Bridge retry to be suppressed (exit 0), not delivered again" >&2
  exit 1
fi
assert_deliveries "$((BEFORE + 1))"
rm -f "$FAKE_HIDE_HISTORY_FILE"

echo "inject dedupe test passed"
