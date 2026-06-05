FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV HOST=0.0.0.0

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    curl \
    wget \
    libfreetype6-dev \
    libpng-dev \
    libjpeg-dev \
    libffi-dev \
    libssl-dev \
    fonts-dejavu \
    fonts-liberation \
    fonts-freefont-ttf \
    fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p analysis_results /tmp/lleo

RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app /tmp/lleo

USER app

EXPOSE 8080

CMD exec uvicorn main:app --host 0.0.0.0 --port 8080 --log-level info --timeout-keep-alive 600
