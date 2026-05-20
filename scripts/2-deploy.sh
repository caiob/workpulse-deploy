#!/usr/bin/env bash
# =============================================================================
# WorkPulse — Etapa 2 de 3: Deploy da infraestrutura
# =============================================================================
# 1. Instala o Terraform (se necessário)
# 2. Inicializa o Terraform com o estado remoto no S3
# 3. Cria o repositório de imagens (ECR) e o CodeBuild
# 4. Builda a imagem Docker do servidor e envia ao ECR
# 5. Cria toda a infraestrutura restante (VPC, ECS, ALB, EFS, CloudFront...)
# 6. Aguarda o servidor ficar saudável
#
# Pode rodar novamente com segurança — o Terraform só aplica o que faltar.
# Leva cerca de 15 a 25 minutos (CloudFront e certificados são os mais lentos).
# =============================================================================
set -euo pipefail

REGION="us-east-1"
LOCK_TABLE="workpulse-tf-locks"
TF_VERSION="1.9.8"
export AWS_DEFAULT_REGION="$REGION"

echo "============================================================"
echo " WorkPulse — Etapa 2 de 3: Deploy"
echo "============================================================"

# --- Localiza as pastas do kit ----------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(dirname "$SCRIPT_DIR")"
INFRA_DIR="$KIT_ROOT/infra"
SERVER_DIR="$KIT_ROOT/server"

# --- Identifica a conta -----------------------------------------------------
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="workpulse-tfstate-${ACCOUNT_ID}"
DEPLOY_BUCKET="workpulse-deploy-${ACCOUNT_ID}"
echo "Conta AWS: $ACCOUNT_ID"

# --- Instala o Terraform se não estiver presente ----------------------------
echo
echo ">> Verificando o Terraform..."
if [ -x "$HOME/bin/terraform" ]; then
  export PATH="$HOME/bin:$PATH"
fi
if ! command -v terraform >/dev/null 2>&1; then
  echo "  - Instalando o Terraform ${TF_VERSION}..."
  mkdir -p "$HOME/bin"
  curl -fsSL -o /tmp/terraform.zip \
    "https://releases.hashicorp.com/terraform/${TF_VERSION}/terraform_${TF_VERSION}_linux_amd64.zip"
  unzip -o /tmp/terraform.zip -d "$HOME/bin" >/dev/null
  rm -f /tmp/terraform.zip
  export PATH="$HOME/bin:$PATH"
fi
echo "  - Terraform: $(terraform version | head -1)"

# --- Inicializa o Terraform -------------------------------------------------
echo
echo ">> Inicializando o Terraform (estado remoto no S3)..."
cd "$INFRA_DIR"
terraform init -input=false -reconfigure \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="key=prod/terraform.tfstate" \
  -backend-config="region=${REGION}" \
  -backend-config="dynamodb_table=${LOCK_TABLE}" \
  -backend-config="encrypt=true"

# --- Passo 1: cria apenas ECR + CodeBuild -----------------------------------
echo
echo ">> [1/3] Criando o repositório de imagens (ECR) e o CodeBuild..."
terraform apply -input=false -auto-approve \
  -target=aws_ecr_repository.server \
  -target=aws_codebuild_project.server

# Nomes determinísticos (não dependem de 'terraform output' após apply parcial).
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/workpulse-prod-server"
CODEBUILD_PROJECT="workpulse-prod-image-build"
echo "  - ECR: $ECR_URI"

# --- Passo 2: envia o código-fonte e builda a imagem ------------------------
echo
echo ">> [2/3] Construindo a imagem Docker do servidor..."
rm -f /tmp/server-src.zip
( cd "$SERVER_DIR" && zip -j -q /tmp/server-src.zip main.py requirements.txt Dockerfile )
aws s3 cp /tmp/server-src.zip "s3://${DEPLOY_BUCKET}/server-src.zip"
echo "  - Código-fonte enviado. Iniciando o build no CodeBuild..."

BUILD_ID="$(aws codebuild start-build --project-name "$CODEBUILD_PROJECT" \
  --query 'build.id' --output text)"
echo "  - Build: $BUILD_ID (aguardando — leva 2 a 4 minutos)"

while true; do
  STATUS="$(aws codebuild batch-get-builds --ids "$BUILD_ID" \
    --query 'builds[0].buildStatus' --output text)"
  if [ "$STATUS" != "IN_PROGRESS" ]; then break; fi
  printf '.'
  sleep 15
done
echo

if [ "$STATUS" != "SUCCEEDED" ]; then
  echo "  ERRO: o build da imagem falhou (status: $STATUS)."
  echo "  Veja os logs em: Console AWS -> CodeBuild -> $CODEBUILD_PROJECT"
  exit 1
fi
echo "  - Imagem construída e publicada no ECR com sucesso."

# --- Passo 3: cria toda a infraestrutura restante ---------------------------
echo
echo ">> [3/3] Criando a infraestrutura completa..."
echo "   (VPC, ECS, ALB, EFS, certificados, CloudFront — pode levar ~15 min)"
terraform apply -input=false -auto-approve

# --- Aguarda o servidor ficar saudável --------------------------------------
echo
echo ">> Aguardando o servidor ficar saudável..."
CLUSTER="$(terraform output -raw ecs_cluster_name)"
SERVICE="$(terraform output -raw ecs_service_name)"
if aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"; then
  echo "  - Servidor no ar e estável."
else
  echo "  AVISO: o servidor ainda não estabilizou. Verifique no Console AWS"
  echo "  -> ECS -> $CLUSTER -> $SERVICE -> aba 'Logs'."
fi

# --- Resumo -----------------------------------------------------------------
API_URL="$(terraform output -raw api_url)"
SITE_URL="$(terraform output -raw site_url)"
echo
echo "============================================================"
echo " Etapa 2 concluída."
echo
echo "   API   : $API_URL"
echo "   Site  : $SITE_URL"
echo
echo " Token de admin (guarde em local seguro):"
echo "   $(terraform output -raw admin_token)"
echo
echo " Próximo passo: bash scripts/3-publicar-site.sh"
echo "============================================================"
