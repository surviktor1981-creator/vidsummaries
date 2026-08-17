FROM python:3.12-slim

# ffmpeg нужен только для локального распознавания речи (ASR_BACKEND),
# но пусть будет в образе — иначе фолбэк молча не заработает.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vidsum/ ./vidsum/
COPY bot.py cli.py ./

# Логи должны идти в docker logs сразу, а не копиться в буфере.
ENV PYTHONUNBUFFERED=1
# База с транскриптами живёт на смонтированном томе, чтобы переживать пересборку.
ENV DB_PATH=/data/vidsum.db
# Модель распознавания весит сотни мегабайт и качается из интернета при первом
# запуске. Кэш — на томе, иначе она скачивалась бы заново после каждой сборки.
ENV HF_HOME=/data/models

CMD ["python", "bot.py"]
