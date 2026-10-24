FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    curl \
    ffmpeg \
    nmap \
    john \
    gobuster \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && pip install uv \
    && pip install yt-dlp \
    && npm install -g yt-dlp-wrap \
    && ln -sf /usr/local/bin/yt-dlp /usr/bin/yt-dlp \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/local/bin:${PATH}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .
COPY mcps.json .
COPY video-audio-mcp/ ./video-audio-mcp/

RUN mkdir -p /app/files && chmod -R 777 /app/files
RUN mkdir -p /data && chmod -R 777 /data

EXPOSE 7860

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]

# ═══════ إضافات جراحية (Ayb-Bh92) — لا تلمس السطور الأصلية أعلاه ═══════
# 1) ترقية Node إلى 22 (يتطلبها Hyperframes)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs

# 2) مكتبات متصفح الرندر (chrome-headless-shell)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# 3) Hyperframes CLI — ما يستدعيه Kino (HTML → فيديو)
RUN npm install -g hyperframes@0.8.167 && (hyperframes telemetry disable || true)
ENV MCP_VIDEO_HYPERFRAMES_COMMAND=/usr/bin/hyperframes

# 4) نسخ مجلد الخطوط (كان ناقصاً في COPY)
COPY fonts/ ./fonts/