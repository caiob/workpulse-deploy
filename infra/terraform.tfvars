# =============================================================================
# WorkPulse — Valores de configuração do deploy
# =============================================================================
# Edite este arquivo se precisar mudar algo. Os valores abaixo já estão
# corretos para o deploy do Caio (workpulse.com.br, us-east-1).
# =============================================================================

aws_region    = "us-east-1"
environment   = "prod"
owner         = "Caio"
public_domain = "workpulse.com.br"

# Tamanho do servidor (ECS Fargate). 512 CPU / 1024 MB atende o início com
# folga. Para aumentar depois: 1024 / 2048, 2048 / 4096, etc.
fargate_cpu       = "512"
fargate_memory    = "1024"
ecs_desired_count = 1

# E-mail que recebe os alertas de monitoramento (erros, indisponibilidade).
# A AWS envia um e-mail de confirmação que precisa ser aceito uma vez.
alert_email = "caiob@hotmail.com"
