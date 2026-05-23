#!/usr/bin/env bash
# =============================================================================
# WorkPulse — Etapa 3 de 3: Publicar o site e os dashboards
# =============================================================================
# Envia os arquivos da pasta site/ para o S3 e limpa o cache do CloudFront.
# Rode esta etapa sempre que alterar algum arquivo HTML do site.
# =============================================================================
set -euo pipefail

REGION="us-east-1"
DOMAIN="workpulse.com.br"
export AWS_DEFAULT_REGION="$REGION"

echo "============================================================"
echo " WorkPulse — Etapa 3 de 3: Publicar o site"
echo "============================================================"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(dirname "$SCRIPT_DIR")"
SITE_DIR="$KIT_ROOT/site"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
SITE_BUCKET="workpulse-site-${ACCOUNT_ID}"

# --- Confere se o bucket existe ---------------------------------------------
if ! aws s3api head-bucket --bucket "$SITE_BUCKET" 2>/dev/null; then
  echo "ERRO: o bucket '$SITE_BUCKET' não existe."
  echo "Rode a etapa 2 (bash scripts/2-deploy.sh) antes desta."
  exit 1
fi

# --- Envia os arquivos ------------------------------------------------------
echo
echo ">> Enviando os arquivos do site para o S3..."
aws s3 sync "$SITE_DIR/" "s3://${SITE_BUCKET}/" --delete \
  --cache-control "public, max-age=3600"

# --- Invalida o cache do CloudFront -----------------------------------------
echo
echo ">> Limpando o cache do CloudFront..."
DIST_ID="$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?contains(Aliases.Items, '${DOMAIN}')].Id | [0]" \
  --output text)"

if [ "$DIST_ID" = "None" ] || [ -z "$DIST_ID" ]; then
  echo "AVISO: distribuição CloudFront não encontrada. O site foi enviado,"
  echo "mas o cache não foi limpo. Rode a etapa 2 se ainda não rodou."
else
  aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
    --paths "/*" >/dev/null
  echo "  - Cache limpo (distribuição $DIST_ID)."
fi

echo
echo "============================================================"
echo " Site publicado!"
echo
echo "   Site institucional : https://${DOMAIN}"
echo "   Painel do cliente  : https://${DOMAIN}/dashboard/workforce.html"
echo "   Console admin      : https://${DOMAIN}/adm/admin.html"
echo "   Documentacao API   : https://${DOMAIN}/docs/developers.html"
echo
echo "   (caminhos antigos -- /workforce.html, /admin.html, /developers.html"
echo "    e /sandbox.html -- continuam funcionando: redirecionam para os novos.)"
echo
echo " Observação: a primeira vez que o CloudFront e o DNS propagam"
echo " pode levar de 15 a 40 minutos. Se o site não abrir de imediato,"
echo " aguarde e tente de novo."
echo "============================================================"
