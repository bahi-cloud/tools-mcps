FROM docker.io/library/python:3.11-slim@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377

COPY requirements.txt .

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/files && chmod -R 777 /app/files

RUN npm install -g hyperframes@0.8.167 && (hyperframes telemetry disable || true)

COPY mcps.json .

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    curl \
    ffmpeg \
    nmap \
    john \
    gobuster \
    hydra \
    nikto \
    sqlmap \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && pip install uv \
    && pip install yt-dlp \
    && npm install -g yt-dlp-wrap \
    && ln -sf /usr/local/bin/yt-dlp /usr/bin/yt-dlp \
    && rm -rf /var/lib/apt/lists/*

COPY *.py .

RUN mkdir -p /data && chmod -R 777 /data

COPY video-audio-mcp/ ./video-audio-mcp/

RUN pip install --no-cache-dir -r requirements.txt

COPY fonts/ ./fonts/