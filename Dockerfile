FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY api ./api
COPY intelligence_report ./intelligence_report
COPY alembic ./alembic
COPY alembic.ini .

RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

EXPOSE 8080
CMD exec uvicorn api:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
