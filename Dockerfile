FROM node:24-alpine AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.11.24 AS uv-bin
FROM python:3.13-slim-bookworm AS officecli-builder
COPY deploy/officecli.json deploy/download_officecli.py /build/
RUN python /build/download_officecli.py --output /officecli

FROM python:3.13-slim-bookworm AS backend-runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy DATA_DIR=/app/data
COPY --from=uv-bin /uv /uvx /bin/
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
RUN .venv/bin/python -c "import openai_codex, codex_cli_bin; from openai_codex import CodexConfig; print('Codex runtime bundled')"
COPY backend/app ./app
RUN groupadd --system --gid 10001 appuser && useradd --system --uid 10001 --gid appuser appuser && mkdir -p /app/data && chown appuser:appuser /app/data
USER appuser

FROM backend-runtime AS worker
USER root
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap libseccomp2 ripgrep libicu72 && rm -rf /var/lib/apt/lists/*
COPY --from=officecli-builder --chmod=755 /officecli /usr/local/bin/officecli
COPY deploy/officecli.json /usr/local/share/officecli/release.json
RUN OFFICECLI_NO_AUTO_INSTALL=1 OFFICECLI_NO_AUTO_RESIDENT=1 OFFICECLI_SKIP_UPDATE=1 officecli --version
USER appuser
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s CMD ["/app/backend/.venv/bin/python", "-m", "app.worker", "--health-check"]
CMD ["/app/backend/.venv/bin/python", "-m", "app.worker"]

FROM worker AS test
USER root
RUN uv sync --frozen --dev --no-install-project
COPY backend/tests ./tests
COPY scripts /app/scripts
USER appuser
CMD ["/app/backend/.venv/bin/python", "-m", "pytest", "-q"]

FROM backend-runtime AS web
COPY --from=frontend-builder /build/frontend/dist /app/frontend/dist
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"]
CMD ["/app/backend/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
