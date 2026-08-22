FROM node:22-bookworm-slim

# 基础依赖：python(prism) / tmux(保活claude) / jq+curl(injector) / bun 稍后装
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      git ca-certificates curl unzip ripgrep procps tmux tini bash jq coreutils \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# Bun：Tidal Echo channel 插件的运行时
RUN curl -fsSL https://bun.sh/install | bash \
    && ln -s /root/.bun/bin/bun /usr/local/bin/bun

# prism-oss：仪表盘（运行时按 PRISM_ENABLED 决定启不启）
RUN git clone --depth 1 https://github.com/lumen-prism/prism-oss /opt/prism \
    && python3 -m venv /opt/prism-venv \
    && /opt/prism-venv/bin/pip install --no-cache-dir -r /opt/prism/requirements.txt

# Tidal Echo：只用它的 channel 插件（relay 后端在另一个服务里，见 tidal-relay/）
RUN git clone --depth 1 https://github.com/anhe2021212-spec/Tidal_Echo /opt/tidal-echo \
    && cd /opt/tidal-echo/channel && bun install

# galatea-garden 唤醒桥（运行时按 WAKE_BRIDGE_ENABLED 决定启不启）
RUN git clone --depth 1 https://github.com/WenXiaoWendy/galatea-garden-wake-bridge /opt/wake-bridge \
    && cd /opt/wake-bridge && npm install && npm run build

ENV HOME=/data/home \
    PRISM_DATA_DIR=/data/prism \
    PORT=8001

COPY entrypoint.sh /entrypoint.sh
COPY inject.sh /opt/injector/inject.sh
RUN chmod +x /entrypoint.sh /opt/injector/inject.sh

EXPOSE 8001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
