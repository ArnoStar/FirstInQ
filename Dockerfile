# --- build: install dependencies into a virtualenv ---------------------------
FROM python:3.13-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Exact versions and hashes from uv.lock (offline).
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.txt

# Downloads go through pip rather than uv so docker/sitecustomize.py can
# shrink TCP segments; that keeps the build working behind VPNs.
COPY docker/sitecustomize.py /build-hooks/
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /app/.venv \
    && PYTHONPATH=/build-hooks /app/.venv/bin/pip install \
        --disable-pip-version-check --require-hashes -r requirements.txt

# --- runtime ------------------------------------------------------------------
FROM python:3.13-slim

RUN useradd --create-home --uid 1000 app \
    && mkdir /data && chown app:app /data

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY app ./app
COPY static ./static

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

USER app
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/time', timeout=3)"]

# One process only: the scheduler runs inside the web server.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
