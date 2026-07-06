#!/usr/bin/env bash
# =============================================================================
# WorkPulse — Destruir a infraestrutura
# =============================================================================
# Remove TODA a infraestrutura criada pelo Terraform (VPC, ECS, ALB, EFS,
# CloudFront, etc.) para parar de gerar custos.
#
# ATENÇÃO: isto APAGA o servidor e os DADOS do banco (volume EFS).
# Os buckets de estado/deploy e a tabela de lock NÃO são removidos.
# =============================================================================
set -euo pipefail

REGION="us-east-1"
LOCK_TABLE="workpulse-tf-locks"
export AWS_DEFAULT_REGION="$REGION"

echo "============================================================"
echo " WorkPulse — DESTRUIR a infraestrutura"
echo "============================================================"
echo
echo " Isto vai APAGAR o servidor, o site e os DADOS (banco no EFS)."
echo
read -r -p ' Para confirmar, digite DESTRUIR e Enter: ' CONFIRMA
if [ "$CONFIRMA" != "DESTRUIR" ]; then
  echo "Cancelado. Nada foi removido."
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(dirname "$SCRIPT_DIR")"
INFRA_DIR="$KIT_ROOT/infra"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="workpulse-tfstate-${ACCOUNT_ID}"

if [ -x "$HOME/bin/terraform" ]; then export PATH="$HOME/bin:$PATH"; fi

cd "$INFRA_DIR"
terraform init -input=false -reconfigure \
  -backend-config="bucket=${STATE_BUCKET}" \
  -backend-config="key=prod/terraform.tfstate" \
  -backend-config="region=${REGION}" \
  -backend-config="dynamodb_table=${LOCK_TABLE}" \
  -backend-config="encrypt=true"

terraform destroy -input=false -auto-approve

echo
echo "============================================================"
echo " Infraestrutura destruída."
echo " Buckets de estado/deploy e a tabela de lock foram mantidos."
echo "============================================================"
