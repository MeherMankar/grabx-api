FROM python:3.11-slim

# curl_cffi needs libcurl + CA certs; gcc for any native builds
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libcurl4 \
    ca-certificates \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# PORT env var is set by Render / Koyeb / Railway automatically.
# Default to 8000 if not set.
ENV PORT=8000

# Gunicorn config:
#   --workers 1        keeps RAM under free-tier limits (~512MB)
#   --threads 4        handles concurrent requests within the single worker
#   --timeout 120      gives streaming proxies 2 min before killing a request
#   --keep-alive 5     reuse connections (faster for bots making many requests)
#   --worker-class gthread  thread-based async (needed for streaming responses)
CMD gunicorn api.index:app \
    --bind 0.0.0.0:${PORT} \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --keep-alive 5 \
    --worker-class gthread \
    --log-level info
