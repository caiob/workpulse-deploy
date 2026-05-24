# =============================================================================
# WorkPulse — Variáveis do Terraform
# =============================================================================
# Os valores reais ficam em terraform.tfvars. Os defaults abaixo já são os
# valores corretos para este deploy; normalmente não é preciso alterar nada
# aqui — edite terraform.tfvars se precisar.
# =============================================================================

variable "aws_region" {
  description = "Região da AWS onde tudo será criado."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Nome do ambiente (usado como sufixo nos recursos)."
  type        = string
  default     = "prod"
}

variable "owner" {
  description = "Responsável pelo projeto (apenas para tags de organização/custo)."
  type        = string
  default     = "Caio"
}

variable "public_domain" {
  description = "Domínio público raiz. Precisa ter uma hosted zone no Route 53 desta conta."
  type        = string
  default     = "workpulse.com.br"
}

variable "fargate_cpu" {
  description = "CPU da task ECS Fargate (512 = 0,5 vCPU)."
  type        = string
  default     = "512"
}

variable "fargate_memory" {
  description = "Memória da task ECS Fargate em MB."
  type        = string
  default     = "1024"
}

variable "ecs_desired_count" {
  description = "Quantidade de instâncias do servidor. Mantenha 1 (SQLite não suporta múltiplos gravadores)."
  type        = number
  default     = 1
}

variable "alert_email" {
  description = "E-mail que recebe os alertas do CloudWatch (latência, erros, etc.)."
  type        = string
  default     = "caiob@hotmail.com"
}
