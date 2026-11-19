# =============================================================================
# WorkPulse Server — Dockerfile multi-stage (PostgreSQL edition)
# =============================================================================

FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt


FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS runtime

ARG VERSION=dev
ARG BUILD_DATE
ARG VCS_REF

LABEL org.opencontainers.image.title="WorkPulse Server" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH=/home/workpulse/.local/bin:$PATH \
    WORKPULSE_LISTEN=0.0.0.0:8080 \
    WEB_CONCURRENCY=2

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN groupadd -r workpulse --gid=1000 \
    && useradd -r -g workpulse --uid=1000 --home-dir=/home/workpulse --shell=/bin/bash workpulse \
    && mkdir -p /home/workpulse /app \
    && chown -R workpulse:workpulse /home/workpulse /app

COPY --from=deps --chown=workpulse:workpulse /root/.local /home/workpulse/.local

WORKDIR /app
COPY --chown=workpulse:workpulse main.py .
COPY --chown=workpulse:workpulse migration.py .
COPY --chown=workpulse:workpulse behavior_analysis.py .
COPY --chown=workpulse:workpulse requirements.txt .

USER workpulse

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# WEB_CONCURRENCY controla o número de workers (default: 2, sobrescrito pelo ECS env)
CMD ["python", "-m", "main"]
