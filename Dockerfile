FROM debian:bookworm-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 python3-venv python3-dev build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MIOT_ROOT=/data \
    MIOT_APP_DIR=/app \
    MIOT_PORT=8080 \
    MIOT_LOG_FILE=/data/miot-cloud.log \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 ca-certificates tzdata passwd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY miot-cloud.py cloud_panel.html admin.html camera.html \
     manifest.webmanifest sw.js /app/
COPY icons /app/icons

RUN groupadd --gid 10001 panel \
    && useradd --uid 10001 --gid panel --home-dir /data --shell /usr/sbin/nologin panel \
    && mkdir -p /data && chown -R panel:panel /data

USER panel
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/status', timeout=3)"]

CMD ["python3", "/app/miot-cloud.py"]
