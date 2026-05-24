#!/usr/bin/env bash
# =============================================================================
# WorkPulse — Atualizar o servidor (re-deploy sem downtime)
# =============================================================================
# Use este script quando alterar o código do servidor (server/main.py).
# Com PostgreSQL, o deploy é ROLLING (zero downtime):
#   a nova task sobe e fica saudável antes de a antiga ser encerrada.
# =============================================================================
set -euo pipefail

REGION="us-east-1"
CODEBUILD_PROJECT="workpulse-prod-image-build"
CLUSTER="workpulse-prod"
SERVICE="workpulse-prod-server"
export AWS_DEFAULT_REGION="$REGION"

echo "============================================================"
echo " WorkPulse — Atualizar o servidor (zero downtime)"
echo "============================================================"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(dirname "$SCRIPT_DIR")"
SERVER_DIR="$KIT_ROOT/server"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
DEPLOY_BUCKET="workpulse-deploy-${ACCOUNT_ID}"

# --- Reconstrói a imagem ---------------------------------------------------
echo
echo ">> Enviando o código e reconstruindo a imagem..."
rm -f /tmp/server-src.zip
( cd "$SERVER_DIR" && zip -j -q /tmp/server-src.zip main.py migration.py requirements.txt Dockerfile )
aws s3 cp /tmp/server-src.zip "s3://${DEPLOY_BUCKET}/server-src.zip"

BUILD_ID="$(aws codebuild start-build --project-name "$CODEBUILD_PROJECT" \
  --query 'build.id' --output text)"
echo "  - Build: $BUILD_ID (aguardando...)"
while true; do
  STATUS="$(aws codebuild batch-get-builds --ids "$BUILD_ID" \
    --query 'builds[0].buildStatus' --output text)"
  if [ "$STATUS" != "IN_PROGRESS" ]; then break; fi
  printf '.'
  sleep 15
done
echo
if [ "$STATUS" != "SUCCEEDED" ]; then
  echo "  ERRO: o build falhou (status: $STATUS)."
  exit 1
fi
echo "  - Nova imagem publicada."

# --- Força o ECS a usar a nova imagem (rolling deploy) ---------------------
echo
echo ">> Atualizando o serviço ECS (rolling deployment sem downtime)..."
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
  --force-new-deployment >/dev/null
echo "  - Aguardando estabilização..."
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"

echo
echo "============================================================"
echo " Servidor atualizado com sucesso (sem downtime)."
echo "============================================================"
