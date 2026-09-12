FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Системные зависимости: ffmpeg нужен для извлечения кадра видео (pHash).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Слой зависимостей: кэшируется, пока не меняется pyproject.toml.
COPY pyproject.toml ./
RUN python -c 'import tomllib; d=tomllib.load(open("pyproject.toml","rb")); open("/tmp/reqs.txt","w").write(chr(10).join(d["project"]["dependencies"]))' \
 && pip install --no-cache-dir -r /tmp/reqs.txt

# Код: пересобирается только этот слой и ниже (секунды при правках .py).
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
# Раскомментируйте, если compose НЕ монтирует ./config в контейнер:
# COPY config ./config

RUN useradd -m appuser \
 && mkdir -p /app/sessions /app/media \
 && chown -R appuser:appuser /app

USER appuser

CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]