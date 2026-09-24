FROM python:3.11-slim

# curl_cffi needs libcurl + CA certs; also need gcc for any native builds
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

# 1 worker to stay within free-tier RAM (~512MB); timeout 60s covers PH scrape + get_media call
CMD ["gunicorn", "api.index:app", "--bind", "0.0.0.0:8000", "--workers", "1", "--timeout", "60", "--worker-class", "gthread", "--threads", "4"]
