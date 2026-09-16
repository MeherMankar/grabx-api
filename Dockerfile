FROM python:3.11

WORKDIR /app
COPY . /app
RUN pip3 install --upgrade pip setuptools wheel
RUN pip3 install --no-cache-dir -r requirements.txt

CMD ["uvicorn", "api.index:app", "--host", "0.0.0.0", "--port", "8000"]
