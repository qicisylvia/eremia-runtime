FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git nginx curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/anhe2021212-spec/Tidal_Echo /opt/tidal-echo \
    && pip install --no-cache-dir -r /opt/tidal-echo/backend/requirements.txt

COPY nginx.conf /etc/nginx/nginx.conf
COPY start.sh /start.sh
RUN chmod +x /start.sh

# relay.db 落在持久卷 /data 上（uvicorn 工作目录）
ENV PORT=8080
EXPOSE 8080
CMD ["/start.sh"]
