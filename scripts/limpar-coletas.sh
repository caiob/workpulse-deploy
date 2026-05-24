#!/usr/bin/env bash
# =============================================================================
# WorkPulse — Console de dados coletados (homologação / reset)
# Usa a API REST do WorkPulse — não precisa de ECS exec nem SSM.
# Uso: bash scripts/limpar-coletas.sh
# =============================================================================
set -uo pipefail

API="https://api.workpulse.com.br"
R='\033[0;31m'; Y='\033[1;33m'; G='\033[0;32m'; C='\033[0;36m'; B='\033[1m'; N='\033[0m'
sep() { echo -e "${B}────────────────────────────────────────────────────${N}"; }

# ── Admin token ───────────────────────────────────────────────────────────
echo ""; sep; echo -e "${B} WorkPulse — Console de dados${N}"; sep

# Tenta obter o token do Terraform, senão pede ao usuário
ADMIN_TOKEN=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")/infra"
if [ -d "$INFRA_DIR" ]; then
  ADMIN_TOKEN=$(cd "$INFRA_DIR" && terraform output -raw admin_token 2>/dev/null || true)
fi
if [ -z "$ADMIN_TOKEN" ]; then
  read -rsp "  Admin token (X-Admin-Token): " ADMIN_TOKEN; echo ""
fi

# ── Verificar conectividade ───────────────────────────────────────────────
echo -e "${C}Verificando conexão com a API...${N}"
if ! curl -sf "$API/healthz" -o /dev/null; then
  echo -e "${R}ERRO: API inacessível em $API${N}"; exit 1
fi
if ! curl -sf -H "X-Admin-Token: $ADMIN_TOKEN" "$API/v1/admin/stats" -o /dev/null; then
  echo -e "${R}ERRO: Token inválido ou sem permissão de admin.${N}"; exit 1
fi
echo -e "  Conectado. Token válido."

# ── Helper curl ───────────────────────────────────────────────────────────
api_get()  { curl -sf -H "X-Admin-Token: $ADMIN_TOKEN" "$API$1"; }
api_post() { curl -sf -X POST -H "X-Admin-Token: $ADMIN_TOKEN" "$API$1"; }

# ── Carregar tenants ──────────────────────────────────────────────────────
load_tenants() {
  TENANTS_JSON=$(api_get "/v1/admin/tenants")
  TENANT_LIST=$(echo "$TENANTS_JSON" | jq -r \
    '.tenants | to_entries[] | "\(.key+1)|\(.value.id)|\(.value.name)|\(.value.plan)|\(.value.status)"')
}

show_tenants() {
  echo ""
  printf "  %-4s %-36s %-24s %-10s %-8s\n" "Nº" "ID" "Nome" "Plano" "Status"
  echo "  ──────────────────────────────────────────────────────────────────────────"
  while IFS='|' read -r NUM ID NAME PLAN STATUS; do
    printf "  %-4s %-36s %-24s %-10s %-8s\n" "$NUM" "$ID" "$NAME" "$PLAN" "$STATUS"
  done <<< "$TENANT_LIST"
  echo ""
}

pick_tenant() {
  show_tenants
  read -rp "  Número do tenant: " TNUM
  PICKED_ID=$(echo "$TENANT_LIST"   | awk -F'|' -v n="$TNUM" '$1==n{print $2}')
  PICKED_NAME=$(echo "$TENANT_LIST" | awk -F'|' -v n="$TNUM" '$1==n{print $3}')
  if [ -z "$PICKED_ID" ]; then echo -e "${R}Número inválido.${N}"; return 1; fi
  return 0
}

load_tenants

# ── MENU PRINCIPAL ────────────────────────────────────────────────────────
while true; do
  echo ""; sep
  echo -e "${B} MENU PRINCIPAL${N}"; sep
  echo -e "  ${B}[1]${N} Listar tenants"
  echo -e "  ${B}[2]${N} Inspecionar dados de um tenant"
  echo -e "  ${B}[3]${N} Limpar dados coletados"
  echo -e "  ${B}[0]${N} Sair"
  echo ""; read -rp "  Opção: " OPT

  # ── [1] Listar ─────────────────────────────────────────────────────────
  if [ "$OPT" = "1" ]; then
    echo ""; sep; echo -e "${B} TENANTS CADASTRADOS${N}"; sep
    load_tenants
    show_tenants

  # ── [2] Inspecionar ────────────────────────────────────────────────────
  elif [ "$OPT" = "2" ]; then
    echo ""; sep; echo -e "${B} SELECIONE O TENANT PARA INSPECIONAR${N}"; sep
    pick_tenant || continue

    while true; do
      echo ""; sep
      echo -e "${B} INSPECIONAR: ${PICKED_NAME}${N}"; sep
      echo -e "  ${B}[1]${N} Usuários detectados"
      echo -e "  ${B}[2]${N} Resumo geral (contagens)"
      echo -e "  ${B}[3]${N} Contagem de eventos por período"
      echo -e "  ${B}[0]${N} Voltar"
      echo ""; read -rp "  Opção: " IOPT

      if [ "$IOPT" = "1" ]; then
        echo ""
        USERS=$(api_get "/v1/users?tenant=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$PICKED_ID'))")")
        echo "$USERS" | jq -r '
          ["USER_ID","NOME","DEPARTAMENTO","LAST_SEEN"],
          ["──────────────────────────────","──────────────────────","──────────────────","───────────────────"],
          (.users[] | [
            .user_id,
            (.display_name // "-"),
            (.department  // "-"),
            .last_seen
          ])
          | @tsv' | awk -F'\t' '{printf "  %-30s %-22s %-18s %s\n",$1,$2,$3,$4}'
        TOTAL=$(echo "$USERS" | jq '.users | length')
        echo -e "\n  Total: ${TOTAL} usuário(s)"

      elif [ "$IOPT" = "2" ]; then
        echo ""
        SUMMARY=$(api_get "/v1/admin/data-summary?tenant=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$PICKED_ID'))")")
        echo "$SUMMARY" | jq -r '
          .tenants[0] |
          "  Usuários        : \(.users)",
          "  Devices         : \(.devices)",
          "  Eventos         : \(.events | tostring | gsub("(?<d>[0-9]{3})"; "\(.d)") )",
          "  Daily summaries : \(.daily_summaries)",
          "  Período         : \(.oldest_event // "-") → \(.newest_event // "-")"'

      elif [ "$IOPT" = "3" ]; then
        echo ""
        SUMMARY=$(api_get "/v1/admin/data-summary?tenant=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$PICKED_ID'))")")
        echo "$SUMMARY" | jq -r '
          .tenants[0] |
          "  Total eventos   : \(.events)",
          "  Primeiro evento : \(.oldest_event // "-")",
          "  Último evento   : \(.newest_event // "-")"'

      elif [ "$IOPT" = "0" ]; then break
      else echo -e "${Y}Opção inválida.${N}"; fi
    done

  # ── [3] Limpar ─────────────────────────────────────────────────────────
  elif [ "$OPT" = "3" ]; then
    echo ""; sep; echo -e "${B} LIMPEZA — ESCOPO${N}"; sep
    echo -e "  ${B}[1]${N} TODOS os tenants"
    echo -e "  ${B}[2]${N} Um tenant específico"
    echo -e "  ${B}[0]${N} Cancelar"
    echo ""; read -rp "  Opção: " SCOPE

    if [ "$SCOPE" = "0" ]; then continue
    elif [ "$SCOPE" = "2" ]; then
      echo ""; sep; echo -e "${B} SELECIONE O TENANT PARA LIMPAR${N}"; sep
      pick_tenant || continue
      CLEAN_ID="$PICKED_ID"; CLEAN_LABEL="$PICKED_NAME"
      PURGE_URL="/v1/admin/purge-data?tenant=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$CLEAN_ID'))")"
    elif [ "$SCOPE" = "1" ]; then
      CLEAN_ID=""; CLEAN_LABEL="TODOS OS TENANTS"
      PURGE_URL="/v1/admin/purge-data"
    else echo -e "${Y}Opção inválida.${N}"; continue; fi

    # Mostrar contagem antes
    echo -e "\n${C}Contando registros...${N}"
    if [ -n "$CLEAN_ID" ]; then
      QPARAM="?tenant=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$CLEAN_ID'))")"
    else
      QPARAM=""
    fi
    BEFORE=$(api_get "/v1/admin/data-summary${QPARAM}")
    echo ""
    printf "  %-28s %10s %10s %10s %14s\n" "Tenant" "users" "devices" "events" "summaries"
    echo "  ──────────────────────────────────────────────────────────────────"
    echo "$BEFORE" | jq -r       '.tenants[] | [.tenant_name, (.users|tostring), (.devices|tostring), (.events|tostring), (.daily_summaries|tostring)] | @tsv'       | awk -F'\t' '{printf "  %-28s %10s %10s %10s %14s\n",$1,$2,$3,$4,$5}' || true
    echo ""

    echo ""
    echo -e "${Y}⚠️  IRREVERSÍVEL — Escopo: ${B}${CLEAN_LABEL}${N}"
    echo -e "   Mantém: tenants, licenças, regras, departamentos, audit_log"
    echo ""
    read -rp "   Digite  LIMPAR  para confirmar: " CONFIRM
    if [ "$CONFIRM" != "LIMPAR" ]; then echo "Cancelado."; continue; fi

    echo -e "\n${C}Executando limpeza via API...${N}"
    RESULT=$(api_post "$PURGE_URL")

    echo ""
    echo "$RESULT" | jq -r '"  Escopo          : "+.scope' || true
    echo "$RESULT" | jq -r '"  events          : "+(.deleted.events|tostring)' || true
    echo "$RESULT" | jq -r '"  daily_summaries : "+(.deleted.daily_summaries|tostring)' || true
    echo "$RESULT" | jq -r '"  devices         : "+(.deleted.devices|tostring)' || true
    echo "$RESULT" | jq -r '"  users           : "+(.deleted.users|tostring)' || true

    echo ""; sep
    echo -e "${G} Limpeza concluida. Agentes podem voltar a coletar normalmente.${N}"
    sep

  elif [ "$OPT" = "0" ]; then echo "Saindo."; exit 0
  else echo -e "${Y}Opcao invalida.${N}"; fi
done
