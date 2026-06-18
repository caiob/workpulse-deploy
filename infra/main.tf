# =============================================================================
# WorkPulse — Infraestrutura AWS (Terraform) — PostgreSQL + RDS
# =============================================================================
# Topologia:
#   Route 53  ─→  CloudFront ─→ S3 .................. site + dashboards (estático)
#   Route 53  ─→  ALB (HTTPS/ACM) ─→ ECS Fargate ─→ RDS PostgreSQL
#   ECR + CodeBuild ............................. build e registro da imagem
#   Secrets Manager ............................. admin token + senha DB + chave de licença
#   CloudWatch + SNS ............................ logs e alertas
#
# Banco de dados: PostgreSQL 15 no RDS (substitui SQLite/EFS).
#   - Múltiplos gravadores → rolling deployment SEM DOWNTIME
#   - ~10-50× mais rápido que SQLite sobre EFS
#   - Backups automáticos de 7 dias + autoscaling de storage
# Região: us-east-1.
# =============================================================================

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.70" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }

  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "workpulse"
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = var.owner
    }
  }
}

# -----------------------------------------------------------------------------
# Data sources
# -----------------------------------------------------------------------------
data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

data "aws_route53_zone" "main" {
  name = var.public_domain
}

# -----------------------------------------------------------------------------
# Locals
# -----------------------------------------------------------------------------
locals {
  azs            = slice(data.aws_availability_zones.available.names, 0, 3)
  account_id     = data.aws_caller_identity.current.account_id
  name_prefix    = "workpulse-${var.environment}"

  vpc_cidr       = "10.0.0.0/16"
  public_subnets = ["10.0.0.0/24", "10.0.1.0/24", "10.0.2.0/24"]
  app_subnets    = ["10.0.10.0/24", "10.0.11.0/24", "10.0.12.0/24"]

  api_domain = "api.${var.public_domain}"
  www_domain = "www.${var.public_domain}"

  deploy_bucket   = "workpulse-deploy-${local.account_id}"
  site_bucket     = "workpulse-site-${local.account_id}"
  releases_bucket = "workpulse-releases-${local.account_id}"
}

# =============================================================================
# 1) REDE — VPC, subnets, gateways, rotas
# =============================================================================

resource "aws_vpc" "main" {
  cidr_block           = local.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.name_prefix}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name_prefix}-igw" }
}

resource "aws_subnet" "public" {
  count                   = 3
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.public_subnets[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags = {
    Name = "${local.name_prefix}-public-${local.azs[count.index]}"
    Tier = "public"
  }
}

resource "aws_subnet" "app" {
  count             = 3
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.app_subnets[count.index]
  availability_zone = local.azs[count.index]
  tags = {
    Name = "${local.name_prefix}-app-${local.azs[count.index]}"
    Tier = "private-app"
  }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name_prefix}-nat-eip" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "${local.name_prefix}-nat" }
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name_prefix}-rt-public" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = { Name = "${local.name_prefix}-rt-private" }
}

resource "aws_route_table_association" "public" {
  count          = 3
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "app" {
  count          = 3
  subnet_id      = aws_subnet.app[count.index].id
  route_table_id = aws_route_table.private.id
}

# =============================================================================
# 2) SECURITY GROUPS
# =============================================================================

resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb"
  description = "ALB - aceita HTTPS/HTTP da internet"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTP (redireciona para HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name_prefix}-alb" }
}

resource "aws_security_group" "ecs" {
  name        = "${local.name_prefix}-ecs"
  description = "ECS tasks - aceita 8080 do ALB, acessa RDS e internet"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "App port vindo do ALB"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    description = "Saida - RDS, ECR, Secrets Manager, CloudWatch"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name_prefix}-ecs" }
}

# Security group do RDS — só aceita conexões vindas do ECS
resource "aws_security_group" "rds" {
  name        = "${local.name_prefix}-rds"
  description = "RDS PostgreSQL - aceita 5432 apenas do ECS"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL vindo das tasks ECS"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }
  tags = { Name = "${local.name_prefix}-rds" }
}

# =============================================================================
# 3) SECRETS — token de admin, senha do banco, chave de licença
# =============================================================================

resource "random_password" "admin_token" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "admin_token" {
  name                    = "${local.name_prefix}/admin-token"
  description             = "Bearer token de administração do WorkPulse"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "admin_token" {
  secret_id     = aws_secretsmanager_secret.admin_token.id
  secret_string = random_password.admin_token.result
}

resource "random_password" "db_password" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "db_password" {
  name                    = "${local.name_prefix}/db-password"
  description             = "Senha do PostgreSQL do WorkPulse"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id     = aws_secretsmanager_secret.db_password.id
  secret_string = random_password.db_password.result
}

# Chave Ed25519 de assinatura de licenças (PEM, base64-encoded).
# O script migrar-dados.sh extrai a chave do EFS e preenche este secret
# ANTES de destruir o EFS. Em instalações novas o servidor gera a chave
# no primeiro boot e a grava aqui automaticamente.
resource "aws_secretsmanager_secret" "license_key" {
  name                    = "${local.name_prefix}/license-key-b64"
  description             = "Chave privada Ed25519 (PEM em base64) de assinatura de licenças"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "license_key" {
  secret_id     = aws_secretsmanager_secret.license_key.id
  secret_string = "PLACEHOLDER"

  # Ignora mudanças feitas externamente (script de migração ou primeiro boot do servidor).
  lifecycle {
    ignore_changes = [secret_string]
  }
}

# =============================================================================
# 4) RDS — PostgreSQL 15 (substitui SQLite/EFS)
# =============================================================================

resource "aws_db_subnet_group" "main" {
  name       = "${local.name_prefix}-db"
  subnet_ids = aws_subnet.app[*].id
  tags       = { Name = "${local.name_prefix}-db" }
}

resource "aws_db_instance" "main" {
  identifier            = "${local.name_prefix}-pg"
  engine                = "postgres"
  engine_version        = "15"
  instance_class        = var.db_instance_class
  allocated_storage     = 20
  max_allocated_storage = 100   # autoscaling automático até 100 GB
  storage_type          = "gp2"
  storage_encrypted     = true

  # Aplica mudanças (ex.: instance_class) JÁ, sem esperar a janela de manutenção.
  # Troca de classe = modify in-place com ~1-2 min de failover (sem perder dados).
  apply_immediately = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db_password.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false

  # Backups automáticos diários com retenção de 7 dias
  backup_retention_period    = 7
  backup_window              = "03:00-04:00"
  maintenance_window         = "sun:04:00-sun:05:00"
  auto_minor_version_upgrade = true

  # Para desenvolvimento — habilite deletion_protection em produção real
  deletion_protection = false
  skip_final_snapshot = true

  # Performance Insights consome ~$25/mês extra — habilite quando houver tráfego real
  performance_insights_enabled = false

  tags = { Name = "${local.name_prefix}-pg" }
}

# =============================================================================
# 5) ECR — registro da imagem do servidor
# =============================================================================

resource "aws_ecr_repository" "server" {
  name                 = "${local.name_prefix}-server"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "server" {
  repository = aws_ecr_repository.server.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Mantem as ultimas 15 imagens"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 15
      }
      action = { type = "expire" }
    }]
  })
}

# =============================================================================
# 6) CODEBUILD — constrói a imagem Docker e envia ao ECR
# =============================================================================

resource "aws_iam_role" "codebuild" {
  name = "${local.name_prefix}-codebuild"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "codebuild" {
  name = "codebuild-permissions"
  role = aws_iam_role.codebuild.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "*"
      },
      {
        Sid    = "ReadSource"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${local.deploy_bucket}",
          "arn:aws:s3:::${local.deploy_bucket}/*"
        ]
      },
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:BatchGetImage"
        ]
        Resource = aws_ecr_repository.server.arn
      }
    ]
  })
}

resource "aws_codebuild_project" "server" {
  name         = "${local.name_prefix}-image-build"
  description  = "Builda a imagem Docker do servidor WorkPulse e envia ao ECR"
  service_role = aws_iam_role.codebuild.arn

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true

    environment_variable {
      name  = "ECR_URI"
      value = aws_ecr_repository.server.repository_url
    }
    environment_variable {
      name  = "AWS_REGION"
      value = var.aws_region
    }
  }

  source {
    type     = "S3"
    location = "${local.deploy_bucket}/server-src.zip"
    buildspec = <<-BUILDSPEC
      version: 0.2
      phases:
        pre_build:
          commands:
            - echo "Autenticando no ECR..."
            - aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $${ECR_URI%/*}
        build:
          commands:
            - echo "Construindo a imagem Docker..."
            - docker build -t $ECR_URI:latest .
        post_build:
          commands:
            - echo "Enviando a imagem para o ECR..."
            - docker push $ECR_URI:latest
            - echo "Imagem publicada com sucesso."
    BUILDSPEC
  }

  logs_config {
    cloudwatch_logs {
      group_name = "/aws/codebuild/${local.name_prefix}-image-build"
    }
  }
}

# =============================================================================
# 7) ECS — cluster, task definition e service (Fargate)
# =============================================================================

resource "aws_ecs_cluster" "main" {
  name = local.name_prefix
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "server" {
  name              = "/aws/ecs/${local.name_prefix}-server"
  retention_in_days = 30
}

resource "aws_iam_role" "ecs_execution" {
  name = "${local.name_prefix}-ecs-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "secrets-access"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [
        aws_secretsmanager_secret.admin_token.arn,
        aws_secretsmanager_secret.db_password.arn,
        aws_secretsmanager_secret.license_key.arn,
      ]
    }]
  })
}

# Role da aplicação: permissão para gravar a chave de licença no primeiro boot
resource "aws_iam_role" "ecs_task" {
  name = "${local.name_prefix}-ecs-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "ecs_task_secrets" {
  name = "license-key-readwrite"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
      ]
      Resource = [aws_secretsmanager_secret.license_key.arn]
    }]
  })
}

# Leitura de custos da AWS (Cost Explorer) para o painel ADM.
# Cost Explorer não suporta restrição por recurso — Resource deve ser "*".
resource "aws_iam_role_policy" "ecs_task_costs" {
  name = "cost-explorer-read"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ce:GetCostAndUsage", "ce:GetCostForecast"]
      Resource = "*"
    }]
  })
}

resource "aws_ecs_task_definition" "server" {
  family                   = "${local.name_prefix}-server"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.fargate_cpu
  memory                   = var.fargate_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  # Sem volume EFS — PostgreSQL no RDS cuida da persistência.

  container_definitions = jsonencode([{
    name      = "server"
    image     = "${aws_ecr_repository.server.repository_url}:latest"
    essential = true

    portMappings = [{ containerPort = 8080, protocol = "tcp" }]

    environment = [
      { name = "WORKPULSE_LISTEN",        value = "0.0.0.0:8080" },
      { name = "WORKPULSE_CORS_ORIGINS",  value = "https://${var.public_domain},https://${local.www_domain}" },
      { name = "DB_HOST",                 value = aws_db_instance.main.address },
      { name = "DB_PORT",                 value = tostring(aws_db_instance.main.port) },
      { name = "DB_NAME",                 value = var.db_name },
      { name = "DB_USER",                 value = var.db_username },
      { name = "WEB_CONCURRENCY",         value = "2" },
      # ARN para o servidor gravar a chave de licença gerada no primeiro boot
      { name = "LICENSE_KEY_SECRET_ARN",  value = aws_secretsmanager_secret.license_key.arn },
      # Bucket privada dos instaladores do agente (releases)
      { name = "WORKPULSE_RELEASES_BUCKET", value = aws_s3_bucket.releases.id },
    ]

    secrets = [
      { name = "WORKPULSE_ADMIN_TOKEN",         valueFrom = aws_secretsmanager_secret.admin_token.arn },
      { name = "DB_PASSWORD",                   valueFrom = aws_secretsmanager_secret.db_password.arn },
      { name = "WORKPULSE_LICENSE_PRIVKEY_B64", valueFrom = aws_secretsmanager_secret.license_key.arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.server.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ecs"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -f http://localhost:8080/healthz || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }

    readonlyRootFilesystem = false
    user                   = "1000:1000"
  }])
}

resource "aws_ecs_service" "server" {
  name             = "${local.name_prefix}-server"
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.server.arn
  desired_count    = var.ecs_desired_count
  launch_type      = "FARGATE"
  platform_version = "LATEST"

  network_configuration {
    subnets          = aws_subnet.app[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.server.arn
    container_name   = "server"
    container_port   = 8080
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # PostgreSQL suporta múltiplos gravadores → rolling deployment SEM DOWNTIME.
  # A task antiga só para depois que a nova está saudável no ALB.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  depends_on = [
    aws_lb_listener.https,
    aws_db_instance.main,
  ]
}

# =============================================================================
# 8) ALB — Application Load Balancer com TLS
# =============================================================================

resource "aws_lb" "main" {
  name                       = local.name_prefix
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  enable_deletion_protection = false
  enable_http2               = true
  drop_invalid_header_fields = true
  tags                       = { Name = local.name_prefix }
}

resource "aws_lb_target_group" "server" {
  name        = "${local.name_prefix}-tg"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/healthz"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 15
    matcher             = "200"
  }

  deregistration_delay = 30
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.alb.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.server.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# =============================================================================
# 9) ACM — certificados TLS
# =============================================================================

resource "aws_acm_certificate" "alb" {
  domain_name       = local.api_domain
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}

resource "aws_acm_certificate" "cdn" {
  domain_name               = var.public_domain
  subject_alternative_names = [local.www_domain]
  validation_method         = "DNS"
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "cert_validation_alb" {
  for_each = {
    for dvo in aws_acm_certificate.alb.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = data.aws_route53_zone.main.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_route53_record" "cert_validation_cdn" {
  for_each = {
    for dvo in aws_acm_certificate.cdn.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = data.aws_route53_zone.main.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "alb" {
  certificate_arn         = aws_acm_certificate.alb.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation_alb : r.fqdn]
}

resource "aws_acm_certificate_validation" "cdn" {
  certificate_arn         = aws_acm_certificate.cdn.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation_cdn : r.fqdn]
}

# =============================================================================
# 10) S3 + CLOUDFRONT — site institucional e dashboards (estáticos)
# =============================================================================

resource "aws_s3_bucket" "site" {
  bucket        = local.site_bucket
  force_destroy = true
  tags          = { Name = "${local.name_prefix}-site" }
}

resource "aws_s3_bucket_public_access_block" "site" {
  bucket                  = aws_s3_bucket.site.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -----------------------------------------------------------------------------
# Bucket PRIVADA dos instaladores do agente (releases). NÃO é pública nem está
# atrás do CloudFront — o download é feito por URL pré-assinada gerada pela API.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket" "releases" {
  bucket = local.releases_bucket
  tags   = { Name = "${local.name_prefix}-releases" }
}

resource "aws_s3_bucket_public_access_block" "releases" {
  bucket                  = aws_s3_bucket.releases.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "releases" {
  bucket = aws_s3_bucket.releases.id
  versioning_configuration { status = "Enabled" }
}

# A task ECS lê/grava os instaladores e gera URLs pré-assinadas.
resource "aws_iam_role_policy" "ecs_task_releases" {
  name = "releases-bucket-rw"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket",
      ]
      Resource = [
        aws_s3_bucket.releases.arn,
        "${aws_s3_bucket.releases.arn}/*",
      ]
    }]
  })
}

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${local.name_prefix}-site-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Clean URLs (viewer-request): reescreve /dashboard, /adm, /docs e paths sem
# extensão para os .html reais no S3. Código em scripts/clean-urls.js.
resource "aws_cloudfront_function" "clean_urls" {
  name    = "${local.name_prefix}-clean-urls"
  runtime = "cloudfront-js-1.0"
  comment = "WorkPulse clean URLs"
  publish = true
  code    = file("${path.module}/../scripts/clean-urls.js")
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  aliases             = [var.public_domain, local.www_domain]
  price_class         = "PriceClass_100"
  comment             = "${local.name_prefix} - site e dashboards"

  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "s3-site"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id       = "s3-site"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.clean_urls.arn
    }

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 3600
    max_ttl     = 86400
  }

  custom_error_response {
    error_code         = 403
    response_code      = 404
    response_page_path = "/index.html"
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.cdn.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }
}

resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontRead"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.site.arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.site.arn
        }
      }
    }]
  })
}

# =============================================================================
# 11) ROUTE 53 — registros DNS
# =============================================================================

resource "aws_route53_record" "api" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = local.api_domain
  type    = "A"
  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "root" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = var.public_domain
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "www" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = local.www_domain
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}

# =============================================================================
# 12) CLOUDWATCH — alertas por e-mail
# =============================================================================

resource "aws_sns_topic" "alerts" {
  name = "${local.name_prefix}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${local.name_prefix}-alb-5xx"
  alarm_description   = "Mais de 10 erros 5xx em 5 minutos"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  treat_missing_data  = "notBreaching"
  dimensions          = { LoadBalancer = aws_lb.main.arn_suffix }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "no_healthy_hosts" {
  alarm_name          = "${local.name_prefix}-sem-hosts-saudaveis"
  alarm_description   = "Sem instâncias saudáveis no ALB"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Minimum"
  threshold           = 1
  treat_missing_data  = "breaching"
  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.server.arn_suffix
  }
  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "ecs_cpu" {
  alarm_name          = "${local.name_prefix}-ecs-cpu-alta"
  alarm_description   = "CPU do servidor acima de 80% por 15 minutos"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  treat_missing_data  = "notBreaching"
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.server.name
  }
  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${local.name_prefix}-rds-cpu-alta"
  alarm_description   = "CPU do banco acima de 80% por 15 minutos"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  treat_missing_data  = "notBreaching"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "rds_storage" {
  alarm_name          = "${local.name_prefix}-rds-storage-baixo"
  alarm_description   = "Menos de 5 GB livres no banco"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FreeStorageSpace"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 5368709120  # 5 GB em bytes
  treat_missing_data  = "notBreaching"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
