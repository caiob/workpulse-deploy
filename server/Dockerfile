# =============================================================================
# WorkPulse Server — Dockerfile multi-stage de produção
# =============================================================================
# Build:
#   docker build -t workpulse/server:latest .
#
# Run:
#   docker run -d --name workpulse \
#     -p 8080:8080 \
#     -v workpulse-data:/data \
#     -e WORKPULSE_DB=/data/workpulse.db \
#     -e WORKPULSE_ADMIN_TOKEN=$(openssl rand -hex 32) \
#     workpulse/server:latest
# =============================================================================

# ----- Stage 1: dependências (cache-friendly) --------------------------------
FROM python:3.11-slim-bookworm AS deps

# Hardening básico do build env
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Apenas requirements primeiro — maximiza cache do Docker
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt


# ----- Stage 2: imagem final, sem build deps ---------------------------------
FROM python:3.11-slim-bookworm AS runtime

ARG VERSION=dev
ARG BUILD_DATE
ARG VCS_REF

LABEL org.opencontainers.image.title="WorkPulse Server" \
      org.opencontainers.image.description="API REST do WorkPulse — privacidade em primeiro lugar" \
      org.opencontainers.image.url="https://workpulse.example" \
      org.opencontainers.image.source="https://github.com/workpulse/workpulse" \
      org.opencontainers.image.vendor="WorkPulse Technologies" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH=/home/workpulse/.local/bin:$PATH \
    WORKPULSE_LISTEN=0.0.0.0:8080 \
    WORKPULSE_DB=/data/workpulse.db

# Atualiza pacotes para pegar patches de segurança e instala apenas curl (healthcheck)
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends curl tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Cria usuário não-root
RUN groupadd -r workpulse --gid=1000 \
    && useradd -r -g workpulse --uid=1000 --home-dir=/home/workpulse --shell=/bin/bash workpulse \
    && mkdir -p /home/workpulse /app /data \
    && chown -R workpulse:workpulse /home/workpulse /app /data

# Copia deps do stage anterior (já como user-installed)
COPY --from=deps --chown=workpulse:workpulse /root/.local /home/workpulse/.local

# Copia código da app
WORKDIR /app
COPY --chown=workpulse:workpulse main.py .
COPY --chown=workpulse:workpulse requirements.txt .

USER workpulse

# Volume para persistência (DB SQLite)
VOLUME ["/data"]

EXPOSE 8080

# Healthcheck via /healthz
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

# tini cuida de signals + reaping de zumbis (melhor que rodar python como PID 1)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "main"]
