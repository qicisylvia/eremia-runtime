FROM python:3.12-slim

ARG TIDAL_ECHO_REF=e7c9bf5c59af873fb8cd2a675a6b9f105ee1d0f7

RUN apt-get update && apt-get install -y --no-install-recommends \
      git nginx curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git init /opt/tidal-echo \
    && git -C /opt/tidal-echo remote add origin https://github.com/anhe2021212-spec/Tidal_Echo \
    && git -C /opt/tidal-echo fetch --depth 1 origin "$TIDAL_ECHO_REF" \
    && git -C /opt/tidal-echo checkout --detach FETCH_HEAD \
    && pip install --no-cache-dir -r /opt/tidal-echo/backend/requirements.txt

# Eremia 的前端装修作为仓库内覆盖层进入镜像；上游固定到上面的 commit，避免构建漂移。
COPY eremia-web/ /opt/eremia-web/
RUN python /opt/eremia-web/customize.py /opt/tidal-echo/web

COPY nginx.conf /etc/nginx/nginx.conf
COPY start.sh /start.sh
RUN chmod +x /start.sh

# relay.db 落在持久卷 /data 上（uvicorn 工作目录）
ENV PORT=8080
EXPOSE 8080
CMD ["/start.sh"]
