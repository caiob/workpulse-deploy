#!/usr/bin/env bash
# =============================================================================
# WorkPulse — Etapa 2 de 3: Deploy da infraestrutura (PostgreSQL edition)
# =============================================================================
#
# ORDEM DE EXECUÇÃO CORRETA:
#
#   Se você está MIGRANDO de SQLite/EFS para PostgreSQL:
#     bash scripts/2-deploy.sh          ← este script faz TUDO na ordem certa:
#                                          Fase A) cria RDS + constrói imagem
#                                          Fase B) migra os dados (SQLite → PG)
#                                          Fase C) troca o ECS + destrói EFS
#     bash scripts/3-publicar-site.sh
#
#   Se é uma instalação NOVA (sem dados para migrar):
#     bash scripts/2-deploy.sh --novo
#     bash scripts/3-publicar-site.sh
#
# Por quê três fases?
#   O terraform apply completo destroi o EFS. Precisamos criar o RDS ANTES
#   de migrar os dados, e só depois trocar o ECS e destruir o EFS.
#
# =============================================================================
set -euo pipefail

REGION="us-east-1"
LOCK_TABLE="workpulse-tf-locks"
TF_VERSION="1.9.8"
MODO_NOVO="${1:-}"   # passe --novo para pular a migração
export AWS_DEFAULT_REGION="$REGION"

echo "============================================================"
echo " WorkPulse — Etapa 2 de 3: Deploy (PostgreSQL)"
echo "============================================================"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(dirname "$SCRIPT_DIR")"
INFRA_DIR="$KIT_ROOT/infra"
SERVER_DIR="$KIT_ROOT/server"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="workpulse-tfstate-${ACCOUNT_ID}"
DEPLOY_BUCKET="workpulse-deploy-${ACCOUNT_ID}"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/workpulse-prod-server"
CODEBUILD_PROJECT="workpulse-prod-image-build"
echo "Conta AWS: $ACCOUNT_ID"

# Detecta se existe EFS com dados (instalação anterior)
EFS_EXISTENTE="$(aws efs describe-file-systems \
  --query "FileSystems[?Tags[?Key=='Name' && Value=='workpulse-prod-data']].FileSystemId | [0]" \
  --output text 2>/dev/null || echo '')"

if [ "$EFS_EXISTENTE" = "None" ] || [ -z "$EFS_EXISTENTE" ]; then
  EFS_EXISTENTE=""
fi

if [ -n "$EFS_EXISTENTE" ] && [ "$MODO_NOVO" != "--novo" ]; then
  echo
  echo "  EFS detectado ($EFS_EXISTENTE) — modo MIGRAÇÃO ativado."
  echo "  O script irá:"
  echo "    Fase A) Criar o RDS e construir a nova imagem"
  echo "    Fase B) Migrar os dados do SQLite para o PostgreSQL"
  echo "    Fase C) Atualizar o ECS para PostgreSQL e destruir o EFS"
  MIGRAR=true
else
  echo
  echo "  Nenhum EFS detectado (ou --novo passado) — modo INSTALAÇÃO NOVA."
  MIGRAR=false
fi

# --- Instala o Terraform se necessário -------------------------------------
echo
echo ">> Verificando o Terraform..."
if [ -x "$HOME/bin/terraform" ]; then export PATH="$HOME/bin:$PATH"; fi
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

# --- Inicializa o Terraform ------------------------------------------------
echo
echo ">> Inicializando o Terraform..."
cd "$INFRA_DIR"
terraform init -input=false -reconfigure \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="key=prod/terraform.tfstate" \
  -backend-config="region=${REGION}" \
  -backend-config="dynamodb_table=${LOCK_TABLE}" \
  -backend-config="encrypt=true"

# ===========================================================================
# FASE A — Cria apenas: ECR, CodeBuild, RDS e secrets novos
#           NÃO toca no ECS (que ainda usa EFS), NÃO destrói EFS
# ===========================================================================
echo
echo ">> [FASE A] Criando ECR, CodeBuild, RDS e secrets..."
terraform apply -input=false -auto-approve \
  -target=aws_ecr_repository.server \
  -target=aws_ecr_lifecycle_policy.server \
  -target=aws_codebuild_project.server \
  -target=aws_iam_role.codebuild \
  -target=aws_iam_role_policy.codebuild \
  -target=aws_db_instance.main \
  -target=aws_db_subnet_group.main \
  -target=aws_security_group.rds \
  -target=random_password.db_password \
  -target=aws_secretsmanager_secret.db_password \
  -target=aws_secretsmanager_secret_version.db_password \
  -target=aws_secretsmanager_secret.license_key \
  -target=aws_secretsmanager_secret_version.license_key \
  -target=aws_secretsmanager_secret.admin_token \
  -target=aws_secretsmanager_secret_version.admin_token \
  -target=random_password.admin_token
echo "  - RDS e secrets criados."

# Constrói a nova imagem (que inclui migration.py)
echo
echo ">> [FASE A] Construindo a nova imagem Docker..."
rm -f /tmp/server-src.zip
( cd "$SERVER_DIR" && zip -j -q /tmp/server-src.zip main.py migration.py requirements.txt Dockerfile )
aws s3 cp /tmp/server-src.zip "s3://${DEPLOY_BUCKET}/server-src.zip"

BUILD_ID="$(aws codebuild start-build --project-name "$CODEBUILD_PROJECT" \
  --query 'build.id' --output text)"
echo "  - Build: $BUILD_ID (aguardando — leva 2 a 4 minutos)"
while true; do
  STATUS="$(aws codebuild batch-get-builds --ids "$BUILD_ID" \
    --query 'builds[0].buildStatus' --output 