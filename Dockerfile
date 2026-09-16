FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml requirements.lock README.md LICENSE ./
COPY app ./app
RUN python -m pip install --upgrade pip && \
    python -m pip wheel --wheel-dir /wheels -r requirements.lock && \
    python -m pip wheel --wheel-dir /wheels --no-deps .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system supportpilot && adduser --system --ingroup supportpilot supportpilot

COPY --from=builder /wheels /wheels
COPY requirements.lock ./
RUN python -m pip install --no-index --find-links=/wheels -r requirements.lock && \
    python -m pip install --no-index --find-links=/wheels --no-deps supportpilot
COPY app ./app
COPY data ./data
COPY domain_packs ./domain_packs
COPY migrations ./migrations
COPY alembic.ini ./
RUN mkdir -p /app/data && chown -R supportpilot:supportpilot /app

USER supportpilot
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
