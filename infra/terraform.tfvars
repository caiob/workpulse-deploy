# =============================================================================
# WorkPulse — Valores de configuração do deploy
# =============================================================================

aws_region    = "us-east-1"
environment   = "prod"
owner         = "Caio"
public_domain = "workpulse.com.br"

# Servidor ECS Fargate — 1 vCPU / 2 GB RAM.
# Para escalar depois: 2048/4096, 4096/8192, etc.
fargate_cpu       = "1024"
fargate_memory    = "2048"
ecs_desired_count = 1

# RDS PostgreSQL — db.t3.small (2 GB RAM, ~$26-30/mês). Subido de micro em
# 2026-06-18 (o micro ficava sem fôlego com rollup + tráfego). Modify in-place.
db_instance_class = "db.t3.small"
db_name           = "workpulse"
db_username       = "workpulse"

# E-mail que recebe os alertas de monitoramento.
alert_email = "caiob@hotmail.com"
