FROM python:3.11-slim

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

CMD ["gunicorn", "api.index:app", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120"]
