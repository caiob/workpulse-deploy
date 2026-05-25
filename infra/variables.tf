# =============================================================================
# WorkPulse — Variáveis do Terraform
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
  description = "Responsável pelo projeto (usado para tags de custo)."
  type        = string
  default     = "Caio"
}

variable "public_domain" {
  description = "Domínio público raiz. Precisa ter uma hosted zone no Route 53 desta conta."
  type        = string
  default     = "workpulse.com.br"
}

variable "fargate_cpu" {
  description = "CPU da task ECS Fargate (1024 = 1 vCPU). Mínimo recomendado com PostgreSQL."
  type        = string
  default     = "1024"
}

variable "fargate_memory" {
  description = "Memória da task ECS Fargate em MB."
  type        = string
  default     = "2048"
}

variable "ecs_desired_count" {
  description = "Quantidade de instâncias do servidor. PostgreSQL suporta múltiplos gravadores."
  type        = number
  default     = 1
}

variable "alert_email" {
  description = "E-mail que recebe os alertas do CloudWatch."
  type        = string
  default     = "caiob@hotmail.com"
}

variable "db_instance_class" {
  description = "Classe da instância RDS PostgreSQL. db.t3.micro (~$15/mês) é suficiente para início."
  type        = string
  default     = "db.t3.micro"
}

variable "db_name" {
  description = "Nome do banco de dados PostgreSQL."
  type        = string
  defaul