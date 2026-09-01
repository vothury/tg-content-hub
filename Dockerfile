FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Слой зависимостей: кэшируется, пока не меняется pyproject.toml.
# Зависимости берутся прямо из него — единый источник правды сохраняется.
COPY pyproject.toml ./
RUN python -c 'import tomllib; d=tomllib.load(open("pyproject.toml","rb")); open("/tmp/reqs.txt","w").write(chr(10).join(d["project"]["dependencies"]))' \
 && pip install --no-cache-dir -r /tmp/reqs.txt

# Код: пересобирается только этот слой и ниже
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-cache-dir --no-deps .

RUN useradd -m appuser \
    && mkdir -p /app/sessions /app/media \
    && chown -R appuser:appuser /app
USER appuser

CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]