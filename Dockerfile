FROM python:3.13-alpine

ARG TASKFLOW_VERSION=0.0.1-dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TASKFLOW_DB=/data/taskflow.db \
    TASKFLOW_VERSION=${TASKFLOW_VERSION}
LABEL org.opencontainers.image.title="TaskFlow" \
      org.opencontainers.image.description="Self-hosted task tracker and daily planner" \
      org.opencontainers.image.version="${TASKFLOW_VERSION}" \
      org.opencontainers.image.source="https://github.com/Benya9669/bny-tskmgr" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

WORKDIR /app
COPY app ./app
COPY web ./web
COPY VERSION ./VERSION
COPY LICENSE ./LICENSE

RUN addgroup -S taskflow && adduser -S taskflow -G taskflow && mkdir /data && chown taskflow:taskflow /data
USER taskflow
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD wget -q -O - http://127.0.0.1:8080/api/health || exit 1
CMD ["python", "-m", "app.server"]
