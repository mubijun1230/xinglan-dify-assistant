FROM python:3.12-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py sql_guard.py schema.json ./

EXPOSE 8787
CMD ["gunicorn", "-b", "0.0.0.0:8787", "-w", "1", "--timeout", "60", "app:app"]
