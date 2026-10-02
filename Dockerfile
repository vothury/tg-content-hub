FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# Системные зависимости: ffmpeg нужен для извлечения кадра видео (pHash).
# Cache-mount'ы: даже если RUN-слой переусполняется (кэш слоёв вытеснен),
# .deb-пакеты берутся из локального кэша, а не скачиваются заново.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends ffmpeg

# Слой зависимостей: кэшируется, пока не меняется pyproject.toml.
# pip cache-mount: при переусполнении слоя колёса ставятся из локального
# кэша (десятки секунд вместо минут скачивания с PyPI).
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python -c 'import tomllib; d=tomllib.load(open("pyproject.toml","rb")); open("/tmp/reqs.txt","w").write(chr(10).join(d["project"]["dependencies"]))' \
 && pip install -r /tmp/reqs.txt

# Код: пересобирается только этот слой и ниже (секунды при правках .py).
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
# Раскомментируйте, если compose НЕ монтирует ./config в контейнер:
# COPY config ./config

RUN useradd -m appuser \
 && mkdir -p /app/sessions /app/media \
 && chown -R appuser:appuser /app

USER appuser

CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]