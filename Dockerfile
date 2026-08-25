FROM node:22-bookworm-slim

# 基础依赖：python(prism/timekeeper) / tmux(保活claude) / jq+curl(injector) / bun 稍后装
RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      git ca-certificates curl unzip ripgrep procps tmux tini bash jq coreutils tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# Bun：Tidal Echo channel 插件的运行时
RUN curl -fsSL https://bun.sh/install | bash \
    && ln -s /root/.bun/bin/bun /usr/local/bin/bun

# prism-oss：仪表盘（运行时按 PRISM_ENABLED 决定启不启）
RUN git clone --depth 1 https://github.com/lumen-prism/prism-oss /opt/prism \
    && python3 -m venv /opt/prism-venv \
    && /opt/prism-venv/bin/pip install --no-cache-dir -r /opt/prism/requirements.txt

# Tidal Echo：只用它的 channel 插件（relay 后端在另一个服务里，见 tidal-relay/）。
# 与 relay 固定同一 commit；小补丁只在送入 Claude 前加上海本地时间，不改聊天存储/展示。
ARG TIDAL_ECHO_REF=e7c9bf5c59af873fb8cd2a675a6b9f105ee1d0f7
COPY patches/tidal-channel-time-context.patch /tmp/tidal-channel-time-context.patch
RUN git init /opt/tidal-echo \
    && git -C /opt/tidal-echo remote add origin https://github.com/anhe2021212-spec/Tidal_Echo \
    && git -C /opt/tidal-echo fetch --depth 1 origin "$TIDAL_ECHO_REF" \
    && git -C /opt/tidal-echo checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/tidal-echo rev-parse HEAD)" = "$TIDAL_ECHO_REF" \
    && git -C /opt/tidal-echo apply /tmp/tidal-channel-time-context.patch \
    && cd /opt/tidal-echo/channel \
    && bun install \
    && bun build server.ts --target=bun --outfile=/tmp/companion-channel-check.js \
    && rm -f /tmp/companion-channel-check.js /tmp/tidal-channel-time-context.patch

# galatea-garden 唤醒桥（运行时按 WAKE_BRIDGE_ENABLED 决定启不启）
# 固定上游版本，避免同一份 Runtime 在不同日期构建出行为不同的桥。
# 本地补丁只关闭 Undici 对 SSE 的 5 分钟 body-idle timeout；真实断线仍 fail-closed，不重连。
ARG WAKE_BRIDGE_REF=f0cd9c27f1b95d6ff8bd8e0f367de7d4518a1c81
COPY patches/wake-bridge-sse.patch /tmp/wake-bridge-sse.patch
RUN git init /opt/wake-bridge \
    && cd /opt/wake-bridge \
    && git remote add origin https://github.com/WenXiaoWendy/galatea-garden-wake-bridge \
    && git fetch --depth 1 origin "$WAKE_BRIDGE_REF" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$WAKE_BRIDGE_REF" \
    && git apply /tmp/wake-bridge-sse.patch \
    && npm ci \
    && npm install --no-save --package-lock=false undici@6.24.1 \
    && npm run typecheck \
    && npm test \
    && rm -f /tmp/wake-bridge-sse.patch

ENV HOME=/data/home \
    PRISM_DATA_DIR=/data/prism \
    TZ=Asia/Shanghai \
    PORT=8001

COPY entrypoint.sh /entrypoint.sh
COPY inject.sh /opt/injector/inject.sh
COPY timekeeper/ /opt/timekeeper/
COPY tests/inject-dedupe-test.sh /tmp/inject-dedupe-test.sh
COPY tests/timekeeper-test.py /tmp/timekeeper-test.py
RUN chmod +x /entrypoint.sh /opt/injector/inject.sh /opt/timekeeper/timekeeper.py /tmp/inject-dedupe-test.sh
RUN echo "[build-test] wake injector dedupe" \
    && /tmp/inject-dedupe-test.sh /opt/injector/inject.sh
RUN echo "[build-test] timekeeper" \
    && python3 -B /tmp/timekeeper-test.py /opt/timekeeper/timekeeper.py
RUN rm -f /tmp/inject-dedupe-test.sh /tmp/timekeeper-test.py

EXPOSE 8001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]
