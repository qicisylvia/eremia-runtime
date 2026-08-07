FROM node:22-bookworm-slim

# Claude Code 运行依赖 + prism 运行依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      git ca-certificates curl ripgrep procps tini bash \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# prism-oss：Claude Code 会话的网页前端（AGPL-3.0）
RUN git clone --depth 1 https://github.com/lumen-prism/prism-oss /opt/prism \
    && python3 -m venv /opt/prism-venv \
    && /opt/prism-venv/bin/pip install --no-cache-dir -r /opt/prism/requirements.txt

# HOME 指向持久卷：~/.claude（会话历史/MCP配置/凭据）和 prism 数据都落在 /data
ENV HOME=/data/home \
    PRISM_DATA_DIR=/data/prism \
    PORT=8001

WORKDIR /opt/prism
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
