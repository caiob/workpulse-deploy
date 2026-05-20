#!/usr/bin/env bash
# =============================================================================
# WorkPulse — Etapa 1 de 3: Bootstrap
# =============================================================================
# Cria os recursos básicos que o Terraform precisa ANTES de existir:
#   - Bucket S3 para o estado do Terraform (com versionamento e criptografia)
#   - Tabela DynamoDB para travar o estado (evita execuções simultâneas)
#   - Bucket S3 para o código-fonte do build da imagem
# E confere se a hosted zone do domínio existe no Route 53.
#
# Pode rodar quantas vezes quiser — se algo já existe, ele apenas avisa.
# =============================================================================
set -euo pipefail

REGION="us-east-1"
DOMAIN="workpulse.com.br"
LOCK_TABLE="workpulse-tf-locks"
export AWS_DEFAULT_REGION="$REGION"

echo "============================================================"
echo " WorkPulse — Etapa 1 de 3: Bootstrap"
echo "============================================================"

# --- Identifica a conta -----------------------------------------------------
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Conta AWS detectada: $ACCOUNT_ID"
STATE_BUCKET="workpulse-tfstate-${ACCOUNT_ID}"
DEPLOY_BUCKET="workpulse-deploy-${ACCOUNT_ID}"

# --- Função auxiliar: cria um bucket S3 privado e seguro --------------------
criar_bucket() {
  local bucket="$1"
  if aws s3api head-bucket --bucket "$bucket" 2>/dev/null; then
    echo "  - Bucket '$bucket' já existe. OK."
    return
  fi
  echo "  - Criando bucket '$bucket'..."
  aws s3api create-bucket --bucket "$bucket" --region "$REGION" >/dev/null
  aws s3api put-bucket-versioning --bucket "$bucket" \
    --versioning-configuration Status=Enabled
  aws s3api put-bucket-encryption --bucket "$bucket" \
    --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  aws s3api put-public-access-block --bucket "$bucket" \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  echo "    Bucket '$bucket' criado."
}

echo
echo ">> Buckets S3..."
criar_bucket "$STATE_BUCKET"
criar_bucket "$DEPLOY_BUCKET"

# --- Tabela DynamoDB de lock ------------------------------------------------
echo
echo ">> Tabela DynamoDB de lock..."
if aws dynamodb describe-table --table-name "$LOCK_TABLE" >/dev/null 2>&1; then
  echo "  - Tabela '$LOCK_TABLE' já existe. OK."
else
  echo "  - Criando tabela '$LOCK_TABLE'..."
  aws dynamodb create-table \
    --table-name "$LOCK_TABLE" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST >/dev/null
  echo "  - Aguardando a tabela ficar ativa..."
  aws dynamodb wait table-exists --table-name "$LOCK_TABLE"
  echo "    Tabela criada."
fi

# --- Verifica a hosted zone do Route 53 -------------------------------------
echo
echo ">> Verificando o domínio no Route 53..."
ZONE_ID="$(aws route53 list-hosted-zones-by-name --dns-name "$DOMAIN" \
  --query "HostedZones[?Name=='${DOMAIN}.'].Id | [0]" --output text)"

if [ "$ZONE_ID" = "None" ] || [ -z "$ZONE_ID" ]; then
  echo "  ATENÇÃO: nenhuma hosted zone encontrada para '$DOMAIN'."
  echo "  O deploy (etapa 2) vai falhar sem ela."
  echo
  echo "  Para criar a hosted zone agora, rode:"
  echo "    aws route53 create-hosted-zone --name $DOMAIN --caller-reference \$(date +%s)"
  echo
  echo "  Depois, copie os 4 nameservers exibidos e configure-os no seu"
  echo "  registrador de domínio (ex.: Registro.br). Aguarde a propagação"
  echo "  antes de rodar a etapa 2."
else
  echo "  - Hosted zone encontrada: $ZONE_ID. OK."
fi

echo
echo "============================================================"
echo " Etapa 1 concluída."
echo "   State bucket : $STATE_BUCKET"
echo "   Deploy bucket: $DEPLOY_BUCKET"
echo "   Lock table   : $LOCK_TABLE"
echo
echo " Próximo passo: bash scripts/2-deploy.sh"
echo "============================================================"
