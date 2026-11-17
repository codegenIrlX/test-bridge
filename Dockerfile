FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

FROM base AS builder
COPY requirements-build.lock requirements-runtime.lock ./
RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: -r requirements-build.lock \
    && python -m pip check \
    && python -m pip wheel --no-cache-dir --no-deps --only-binary=:all: \
        --wheel-dir /wheels -r requirements-runtime.lock
COPY pyproject.toml ./
COPY metrics_srv/ metrics_srv/
RUN python -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /wheels .

FROM base AS runtime
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --no-index --no-deps /wheels/*.whl \
    && python -m pip check \
    && useradd --create-home --uid 10001 metrics \
    && mkdir -p /data/imports \
    && chown metrics:metrics /data/imports
COPY config.yaml ./
COPY deploy/ deploy/

USER metrics
CMD ["python", "-m", "metrics_srv", "api"]
