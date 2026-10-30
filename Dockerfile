FROM python:3.11-slim

# System deps for TgCrypto
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent volume mount point
RUN mkdir -p /app/data/downloads

ENV DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
