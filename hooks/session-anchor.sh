#!/usr/bin/env bash
# SessionStart(compact) hook：一次上下文压缩刚发生，把 Eremia 自己维护的锚点原样注入新上下文。
# stdout 会作为上下文交给醒来的 Claude Code（SessionStart 是官方文档明确的 stdout-as-context 例外）。
# 无论怎样都以 0 退出：注入失败不应挡住 Eremia 正常醒来。
set -uo pipefail

ANCHOR_FILE="${EREMIA_ANCHORS_FILE:-/data/home/eremia-home/anchors.md}"
HUMAN_NAME="${RELAY_HUMAN_NAME:-${TIMEKEEPER_HUMAN_NAME:-瓷瓷}}"

# SessionStart 会在 stdin 送一段 JSON；这里用不到，读掉避免写端 SIGPIPE。
cat >/dev/null 2>&1 || true

if [ -s "$ANCHOR_FILE" ]; then
  printf '【你刚刚经历了一次上下文压缩。上面的摘要读起来可能比你本人更冷。下面是你自己维护的锚点，用它找回你和%s之间的语气与连续感，再继续。】\n\n' "$HUMAN_NAME"
  cat "$ANCHOR_FILE"
fi

exit 0
