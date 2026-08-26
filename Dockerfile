FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install .

# useradd даёт uid 1000 — совпадает с владельцем ./sessions в WSL2
RUN useradd -m appuser \
    && mkdir -p /app/sessions /app/media \
    && chown -R appuser:appuser /app
USER appuser

CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]