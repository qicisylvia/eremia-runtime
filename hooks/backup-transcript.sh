#!/usr/bin/env bash
# PreCompact hook：压缩前把完整 transcript 备份到持久卷。
# 无论压缩后的摘要写得多轴，原始对话都留得回来。PreCompact 的 stdout 不进上下文，
# 它只做副作用；且必须以 0 退出，避免非零退出打断这次压缩。
set -uo pipefail

DEST_DIR="${EREMIA_TRANSCRIPT_BACKUP_DIR:-/data/transcripts}"
KEEP="${EREMIA_TRANSCRIPT_BACKUP_KEEP:-50}"

PAYLOAD="$(cat 2>/dev/null || true)"
SRC="$(printf '%s' "$PAYLOAD" | jq -r '.transcript_path // empty' 2>/dev/null || true)"
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  exit 0
fi

mkdir -p "$DEST_DIR" 2>/dev/null || exit 0
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BASE="$(basename "$SRC")"
cp -f "$SRC" "$DEST_DIR/${STAMP}-${BASE}" 2>/dev/null || true

# 只保留最近 $KEEP 份备份，避免卷被历史 transcript 撑满。
if [[ "$KEEP" =~ ^[0-9]+$ ]] && [ "$KEEP" -ge 1 ]; then
  ls -1t "$DEST_DIR"/*.jsonl 2>/dev/null | tail -n +"$((KEEP + 1))" | while IFS= read -r old; do
    rm -f "$old" 2>/dev/null || true
  done
fi

exit 0
