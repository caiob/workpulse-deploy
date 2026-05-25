#!/usr/bin/env bash
# =============================================================================
# WorkPulse — Migrar dados do SQLite/EFS para o PostgreSQL/RDS
# =============================================================================
#
# O que este script faz:
#   1. Extrai a chave de licença Ed25519 do container atual (EFS) e a salva
#      no Secrets Manager (para o novo servidor usar a mesma chave).
#   2. Registra uma task definition temporária que monta o EFS antigo E
#      tem acesso ao novo RDS.
#   3. Roda migration.py dentro dessa task (lê SQLite, escreve no PG).
#   4. Aguarda o término e exibe os logs.
#
# Execute ANTES de fazer `terraform apply` com o novo main.tf, ou logo
# depois — desde que o EFS ainda exista (não tenha sido destruído).
#
# Pré-requisitos:
#   - AWS CLI configurado com permissões de ECS, Secrets Manager, EFS e RDS
#   - Terraform já aplicado (para obter os outputs do RDS e do ECS)
#   - Infraestrutura anterior ainda de pé (EFS com os dados)
# =============================================================================
set -euo pipefail

REGION="us-east-1"
CLUSTER="workpulse-prod"
export AWS_DEFAULT_REGION="$REGION"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(dirname "$SCRIPT_DIR")"
INFRA_DIR="$KIT_ROOT/infra"

echo "============================================================"
echo " WorkPulse — Migração SQLite → PostgreSQL"
echo "============================================================"

# --- Coleta dados da infra via terraform output ----------------------------
echo
echo ">> Lendo dados da infraestrutura (terraform output)..."
cd "$INFRA_DIR"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/workpulse-prod-server"

# IDs dos recursos — pega do estado Terraform se disponível, senão usa defaults
DB_HOST="$(terraform output -raw rds_endpoint 2>/dev/null | cut -d: -f1 || echo '')"
if [ -z "$DB_HOST" ]; then
  echo "  ERRO: não foi possível ler 'rds_endpoint' do Terraform."
  echo "  Certifique-se de rodar 'terraform apply' com o novo main.tf antes deste script."
  exit 1
fi
DB_PORT="5432"
DB_NAME="workpulse"
DB_USER="workpulse"

ADMIN_TOKEN_ARN="arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:workpulse-prod/admin-token"
DB_PASSWORD_ARN="arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:workpulse-prod/db-password"
LICENSE_KEY_ARN="$(terraform output -raw license_key_secret_arn 2>/dev/null || \
  echo "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:workpulse-prod/license-key-b64")"

ECS_EXECUTION_ROLE="arn:aws:iam::${ACCOUNT_ID}:role/workpulse-prod-ecs-execution"
ECS_TASK_ROLE="arn:aws:iam::${ACCOUNT_ID}:role/workpulse-prod-ecs-task"

# Subnets e security groups do ECS
ECS_SG="$(aws ec2 describe-security-groups \
  --filters Name=group-name,Values=workpulse-prod-ecs \
  --query 'SecurityGroups[0].GroupId' --output text)"

APP_SUBNETS="$(aws ec2 describe-subnets \
  --filters Name=tag:Tier,Values=private-app Name=tag:Project,Values=workpulse \
  --query 'Subnets[*].SubnetId' --output text | tr '\t' ',')"

# EFS antigo
EFS_ID="$(aws efs describe-file-systems \
  --query "FileSystems[?Tags[?Key=='Name' && Value=='workpulse-prod-data']].FileSystemId | [0]" \
  --output text 2>/dev/null || echo '')"

echo "  RDS Host   : $DB_HOST"
echo "  ECS SG     : $ECS_SG"
echo "  EFS ID     : ${EFS_ID:-NÃO ENCONTRADO}"

# ---------------------------------------------------------------------------
# PASSO 1 — Extrair chave de licença do container atual e salvar no SM
# ---------------------------------------------------------------------------
echo
echo ">> [1/3] Extraindo a chave de licença Ed25519 do container atual..."

# Tenta via ECS Exec no container atual
CURRENT_TASK="$(aws ecs list-tasks --cluster "$CLUSTER" \
  --service-name workpulse-prod-server \
  --query 'taskArns[0]' --output text 2>/dev/null || echo '')"

KEY_EXTRACTED=false
if [ -n "$CURRENT_TASK" ] && [ "$CURRENT_TASK" != "None" ]; then
  echo "  Task atual: $CURRENT_TASK"
  echo "  Tentando extrair chave via ECS Exec..."

  # Habilita ECS Exec se ainda não estiver habilitado
  aws ecs update-service --cluster "$CLUSTER" --service workpulse-prod-server \
    --enable-execute-command >/dev/null 2>&1 || true

  sleep 5

  # Extrai a chave do arquivo no EFS
  KEY_B64="$(aws ecs execute-command \
    --cluster "$CLUSTER" \
    --task "$CURRENT_TASK" \
    --container server \
    --interactive \
    --command "python3 -c \"
import base64, os
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
key_path = '/data/license_priv.key'
if os.path.exists(key_path):
    with open(key_path, 'rb') as f: pem = f.read()
    print(base64.b64encode(pem).decode())
else:
    print('NOT_FOUND')
\"" 2>/dev/null | grep -v "^Starting" | grep -v "^The Session" | tail -1 || echo '')"

  if [ -n "$KEY_B64" ] && [ "$KEY_B64" != "NOT_FOUND" ] && [ "$KEY_B64" != "" ]; then
    echo "  Chave extraída com sucesso. Salvando no Secrets Manager..."
    aws secretsmanager put-secret-value \
      --secret-id "$LICENSE_KEY_ARN" \
      --secret-string "$KEY_B64"
    echo "  Chave de licença salva."
    KEY_EXTRACTED=true
  fi
fi

if [ "$KEY_EXTRACTED" = "false" ]; then
  echo
  echo "  AVISO: não foi possível extrair a chave de licença automaticamente."
  echo "  Isso significa que o novo servidor irá GERAR UMA NOVA CHAVE Ed25519."
  echo
  echo "  Consequência: as licenças existentes dos agentes NÃO funcionarão"
  echo "  com a nova chave. Você precisará reemitir as licenças após a migração."
  echo
  read -r -p "  Deseja continuar mesmo assim? (s/N): " CONFIRM
  if [[ "$CONFIRM" != "s" && "$CONFIRM" != "S" ]]; then
    echo "  Migração cancelada pelo usuário."
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# PASSO 2 — Registrar task definition temporária de migração
# ---------------------------------------------------------------------------
echo
echo ">> [2/3] Registrando task definition de migração..."

if [ -z "$EFS_ID" ] || [ "$EFS_ID" = "None" ]; then
  echo "  AVISO: EFS não encontrado. Migração de dados não será possível."
  echo "  Os dados do banco antigo não serão migrados."
  echo "  Se quiser migrar os dados manualmente, exporte o workpulse.db"
  echo "  e execute: python migration.py com as variáveis de DB configuradas."
  EFS_VOLUME_JSON="[]"
  MOUNT_POINTS_JSON="[]"
else
  EFS_VOLUME_JSON="[{
    \"name\": \"efs-data\",
    \"efsVolumeConfiguration\": {
      \"fileSystemId\": \"$EFS_ID\",
      \"transitEncryption\": \"ENABLED\"
    }
  }]"
  MOUNT_POINTS_JSON="[{\"sourceVolume\": \"efs-data\", \"containerPath\": \"/data\", \"readOnly\": true}]"
fi

# DB_PASSWORD em tempo de execução (do Secrets Manager)
TASK_DEF_JSON=$(cat <<EOF
{
  "family": "workpulse-migration",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "executionRoleArn": "$ECS_EXECUTION_ROLE",
  "taskRoleArn": "$ECS_TASK_ROLE",
  "volumes": $EFS_VOLUME_JSON,
  "containerDefinitions": [{
    "name": "migration",
    "image": "$ECR_URI:latest",
    "essential": true,
    "mountPoints": $MOUNT_POINTS_JSON,
    "command": ["python", "migration.py"],
    "environment": [
      {"name": "DB_HOST",    "value": "$DB_HOST"},
      {"name": "DB_PORT",    "value": "$DB_PORT"},
      {"name": "DB_NAME",    "value": "$DB_NAME"},
      {"name": "DB_USER",    "value": "$DB_USER"}
    ],
    "secrets": [
      {"name": "DB_PASSWORD",           "valueFrom": "$DB_PASSWORD_ARN"},
      {"name": "WORKPULSE_ADMIN_TOKEN", "valueFrom": "$ADMIN_TOKEN_ARN"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/aws/ecs/workpulse-prod-server",
        "awslogs-region": "$REGION",
        "awslogs-stream-prefix": "migration"
      }
    }
  }]
}
EOF
)

TASK_DEF_ARN="$(echo "$TASK_DEF_JSON" | aws ecs register-task-definition \
  --cli-input-json file:///dev/stdin \
  --query 'taskDefinition.taskDefinitionArn' --output text)"

echo "  Task definition registrada: $TASK_DEF_ARN"

# ---------------------------------------------------------------------------
# PASSO 3 — Rodar a task de migração
# ---------------------------------------------------------------------------
echo
echo ">> [3/3] Executando a migração..."

SUBNETS_JSON=$(echo "$APP_SUBNETS" | python3 -c "
import sys, json
subnets = sys.stdin.read().strip().split(',')
print(json.dumps(subnets))
")

TASK_ARN="$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$TASK_DEF_ARN" \
  --launch-type FARGATE \
  --network-configuration "{
    \"awsvpcConfiguration\": {
      \"subnets\": $SUBNETS_JSON,
      \"securityGroups\": [\"$ECS_SG\"],
      \"assignPublicIp\": \"DISABLED\"
    }
  }" \
  --query 'tasks[0].taskArn' --output text)"

echo "  Task de migração iniciada: $TASK_ARN"
echo "  Aguardando conclusão (pode levar alguns minutos)..."

# Aguarda a task terminar
while true; do
  STATUS="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
    --query 'tasks[0].lastStatus' --output text)"
  EXIT_CODE="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
    --query 'tasks[0].containers[0].exitCode' --output text 2>/dev/null || echo 'null')"

  if [ "$STATUS" = "STOPPED" ]; then
    break
  fi
  printf "."
  sleep 10
done
echo

if [ "$EXIT_CODE" = "0" ]; then
  echo "  Migração concluída com sucesso! (exit code: 0)"
else
  echo "  ERRO: a migração terminou com exit code $EXIT_CODE."
  echo "  Veja os logs em:"
  echo "  Console AWS → CloudWatch → /aws/ecs/workpulse-prod-server → prefixo 'migration/'"
fi

# Exibe os últimos logs do CloudWatch
echo
echo ">> Últimas linhas do log de migração:"
LOG_STREAM="migration/migration/$(echo "$TASK_ARN" | rev | cut -d'/' -f1 | rev)"
aws logs get-log-events \
  --log-group-name "/aws/ecs/workpulse-prod-server" \
  --log-stream-name "$LOG_STREAM" \
  --query 'events[*].message' --output text 2>/dev/null \
  | head -50 || echo "  (logs não disponíveis ainda — aguarde e consulte no CloudWatch)"

# Limpa a task definition temporária
aws ecs deregister-task-definition --task-definition "$TASK_DEF_ARN" >/dev/null 2>&1 || true

echo
echo "============================================================"
if [ "$EXIT_CODE" = "0" ]; then
  echo " Migração de dados concluída!"
  echo
  echo " Próximos passos:"
  echo "  1. Verifique os dados no novo dashboard"
  echo "  2. Se tudo estiver ok, destrua os recursos EFS antigos:"
  echo "     O 'terraform apply' com o novo main.tf já não cria EFS,"
  echo "     mas os recursos EFS existentes precisam ser removidos manualmente:"
  echo
  echo "     aws efs delete-access-point --access-point-id <AP_ID>"
  echo "     aws efs delete-mount-target --mount-target-id <MT_ID> (para cada AZ)"
  echo "     aws efs delete-file-system --file-system-id $EFS_ID"
  echo "     aws ec2 delete-security-group --group-id <SG_EFS_ID>"
else
  echo " Migração falhou — veja os logs no CloudWatch."
fi
echo "============================================================"
