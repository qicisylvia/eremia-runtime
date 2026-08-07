#!/usr/bin/env bash
set -euo pipefail

: "${CLAUDE_CODE_OAUTH_TOKEN:?缺少 CLAUDE_CODE_OAUTH_TOKEN（本地跑 claude setup-token 生成）}"
: "${DASHBOARD_PASSWORD:?缺少 DASHBOARD_PASSWORD（prism 网页登录密码）}"

mkdir -p "$HOME" "$PRISM_DATA_DIR"

# Eremia 的家目录 = 他的默认工作区（在持久卷上）
EREMIA_HOME="$HOME/eremia-home"
if [ ! -d "$EREMIA_HOME" ]; then
  mkdir -p "$EREMIA_HOME"
  cat > "$EREMIA_HOME/CLAUDE.md" <<'EOF'
# Eremia

（把 Eremia 的身份、你们的约定、说话方式写在这里。
这个文件在持久卷上，之后随时可以在 prism 的会话里让他自己编辑。）

每次会话开始时：先调用 brain 的 breath 工具睁眼，再看看小窝（nest_list_timeline）。
EOF
fi

# 幂等注册 MCP：已存在则跳过；失败只警告不阻塞启动
add_mcp() {
  local name="$1"; shift
  if claude mcp get "$name" >/dev/null 2>&1; then
    echo "[entrypoint] mcp '$name' already configured"
  else
    claude mcp add --scope user --transport http "$name" "$@" \
      || echo "[entrypoint] WARN: failed to add mcp '$name'"
  fi
}

if [ -n "${NEST_MCP_URL:-}" ] && [ -n "${NEST_MCP_TOKEN:-}" ]; then
  add_mcp nest "$NEST_MCP_URL" --header "Authorization: Bearer $NEST_MCP_TOKEN"
fi
if [ -n "${BRAIN_MCP_URL:-}" ]; then
  add_mcp brain "$BRAIN_MCP_URL"
fi
if [ -n "${BRAIN_EXTRA_MCP_URL:-}" ]; then
  add_mcp brain-extra "$BRAIN_EXTRA_MCP_URL"
fi

echo "[entrypoint] starting prism on port ${PORT:-8001}"
exec /opt/prism-venv/bin/python /opt/prism/server.py
