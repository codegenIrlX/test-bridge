FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 metrics \
    && mkdir -p /data/imports \
    && chown metrics:metrics /data/imports

USER metrics
CMD ["python", "-m", "metrics_srv", "api"]
