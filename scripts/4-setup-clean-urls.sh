#!/usr/bin/env bash
# =============================================================================
# WorkPulse — Etapa 4 (opcional): Configurar Clean URLs via CloudFront Function
# =============================================================================
# Cria (ou atualiza) a CloudFront Function "workpulse-clean-urls", publica-a
# no stage LIVE e a associa como viewer-request na distribuição do domínio.
#
# Após rodar este script, os usuários podem acessar:
#   https://workpulse.com.br/dashboard   (em vez de /dashboard/workforce.html)
#   https://workpulse.com.br/adm         (em vez de /adm/admin.html)
#   https://workpulse.com.br/docs        (em vez de /docs/developers.html)
#
# Pré-requisito: bash scripts/2-deploy.sh já rodou (distribuição existe).
# =============================================================================
set -euo pipefail

REGION="us-east-1"
DOMAIN="workpulse.com.br"
FUNC_NAME="workpulse-clean-urls"
export AWS_DEFAULT_REGION="$REGION"

echo "============================================================"
echo " WorkPulse — Configurando Clean URLs"
echo "============================================================"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FUNC_CODE="$SCRIPT_DIR/clean-urls.js"

if [ ! -f "$FUNC_CODE" ]; then
  echo "ERRO: arquivo '$FUNC_CODE' não encontrado."
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. Cria ou atualiza a CloudFront Function
# ---------------------------------------------------------------------------
echo
echo ">> Verificando a função '$FUNC_NAME'..."

EXISTING_ARN="$(aws cloudfront describe-function \
  --name "$FUNC_NAME" \
  --query 'FunctionSummary.FunctionMetadata.FunctionARN' \
  --output text 2>/dev/null || echo '')"

if [ -z "$EXISTING_ARN" ] || [ "$EXISTING_ARN" = "None" ]; then
  echo "  - Função não encontrada, criando..."
  CREATE_OUT="$(aws cloudfront create-function \
    --name "$FUNC_NAME" \
    --function-config 'Comment=WorkPulse clean URLs,Runtime=cloudfront-js-1.0' \
    --function-code "fileb://${FUNC_CODE}")"
  FUNC_ARN="$(echo "$CREATE_OUT" | python3 -c \
    "import sys,json; print(json.load(sys.stdin)['FunctionSummary']['FunctionMetadata']['FunctionARN'])")"
  echo "  - Função criada: $FUNC_ARN"
else
  echo "  - Função encontrada, atualizando código..."
  FUNC_ARN="$EXISTING_ARN"
  ETAG_UP="$(aws cloudfront describe-function \
    --name "$FUNC_NAME" --query 'ETag' --output text)"
  aws cloudfront update-function \
    --name "$FUNC_NAME" \
    --function-config 'Comment=WorkPulse clean URLs,Runtime=cloudfront-js-1.0' \
    --function-code "fileb://${FUNC_CODE}" \
    --if-match "$ETAG_UP" > /dev/null
  echo "  - Código atualizado."
fi

# ---------------------------------------------------------------------------
# 2. Publica a função (DEVELOPMENT → LIVE)
# ---------------------------------------------------------------------------
echo
echo ">> Publicando a função (DEVELOPMENT → LIVE)..."
ETAG_PUB="$(aws cloudfront describe-function \
  --name "$FUNC_NAME" --query 'ETag' --output text)"
aws cloudfront publish-function \
  --name "$FUNC_NAME" \
  --if-match "$ETAG_PUB" > /dev/null
echo "  - Função publicada no stage LIVE."

# ---------------------------------------------------------------------------
# 3. Localiza a distribuição CloudFront
# ---------------------------------------------------------------------------
echo
echo ">> Localizando a distribuição CloudFront para '${DOMAIN}'..."
DIST_ID="$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?contains(Aliases.Items, '${DOMAIN}')].Id | [0]" \
  --output text)"

if [ "$DIST_ID" = "None" ] || [ -z "$DIST_ID" ]; then
  echo "ERRO: distribuição CloudFront não encontrada para '${DOMAIN}'."
  echo "Rode a etapa 2 (bash scripts/2-deploy.sh) antes desta."
  exit 1
fi
echo "  - Distribuição: $DIST_ID"

# ---------------------------------------------------------------------------
# 4. Associa a função como viewer-request no DefaultCacheBehavior
# ---------------------------------------------------------------------------
echo
echo ">> Associando a função à distribuição..."
CONFIG_JSON="$(aws cloudfront get-distribution-config --id "$DIST_ID")"
DIST_ETAG="$(echo "$CONFIG_JSON" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['ETag'])")"

UPDATED_CONFIG="$(python3 -c "
import sys, json

data = json.loads(sys.stdin.read())
func_arn = '${FUNC_ARN}'
cfg = data['DistributionConfig']
dcb = cfg['DefaultCacheBehavior']

# Garante que FunctionAssociations existe
if 'FunctionAssociations' not in dcb:
    dcb['FunctionAssociations'] = {'Quantity': 0, 'Items': []}

# Remove associação viewer-request existente (se houver) e adiciona a nova
items = [x for x in dcb['FunctionAssociations'].get('Items', [])
         if x.get('EventType') != 'viewer-request']
items.append({'FunctionARN': func_arn, 'EventType': 'viewer-request'})
dcb['FunctionAssociations']['Items'] = items
dcb['FunctionAssociations']['Quantity'] = len(items)

print(json.dumps(cfg))
" <<< "$CONFIG_JSON")"

aws cloudfront update-distribution \
  --id "$DIST_ID" \
  --distribution-config "$UPDATED_CONFIG" \
  --if-match "$DIST_ETAG" > /dev/null

echo "  - Função associada com sucesso."

# ---------------------------------------------------------------------------
# 5. Aguarda propagação (opcional — não bloqueia o terminal)
# ---------------------------------------------------------------------------
echo
echo "  Distribuição em atualização (leva ~3-5 min para propagar)."
echo "  Você pode rodar o comando abaixo para acompanhar:"
echo "    aws cloudfront wait distribution-deployed --id $DIST_ID"

echo
echo "============================================================"
echo " Clean URLs configuradas!"
echo
echo "   https://${DOMAIN}            → index.html"
echo "   https://${DOMAIN}/dashboard  → /dashboard/workforce.html"
echo "   https://${DOMAIN}/adm        → /adm/admin.html"
echo "   https://${DOMAIN}/docs       → /docs/developers.html"
echo
echo "   Qualquer outro path sem extensão também funciona:"
echo "   https://${DOMAIN}/foo/bar    → /foo/bar.html"
echo
echo "   Arquivos com extensão (.js, .css, .png, etc.) passam"
echo "   direto sem modificação — assets não são afetados."
echo "============================================================"
