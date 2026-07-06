# =============================================================================
# WorkPulse — Saídas do Terraform
# =============================================================================

output "api_url" {
  description = "URL pública da API do WorkPulse."
  value       = "https://api.${var.public_domain}"
}

output "site_url" {
  description = "URL pública do site institucional."
  value       = "https://${var.public_domain}"
}

output "alb_dns_name" {
  description = "DNS interno do Application Load Balancer."
  value       = aws_lb.main.dns_name
}

output "cloudfront_domain" {
  description = "Domínio do CloudFront."
  value       = aws_cloudfront_distribution.site.domain_name
}

output "cloudfront_distribution_id" {
  description = "ID do CloudFront — usado para invalidar o cache ao publicar o site."
  value       = aws_cloudfront_distribution.site.id
}

output "ecr_repository_url" {
  description = "URL do repositório ECR da imagem do servidor."
  value       = aws_ecr_repository.server.repository_url
}

output "codebuild_project_name" {
  description = "Nome do projeto CodeBuild."
  value       = aws_codebuild_project.server.name
}

output "site_bucket_name" {
  description = "Bucket S3 do site."
  value       = aws_s3_bucket.site.bucket
}

output "ecs_cluster_name" {
  description = "Nome do cluster ECS."
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "Nome do serviço ECS."
  value       = aws_ecs_service.server.name
}

output "rds_endpoint" {
  description = "Endpoint do PostgreSQL (host:port) — acessível apenas de dentro da VPC."
  value       = "${aws_db_instance.main.address}:${aws_db_instance.main.port}"
}

output "rds_identifier" {
  description = "Identificador da instância RDS."
  value       = aws_db_instance.main.identifier
}

output "admin_token" {
  description = "Token de administração — necessário para entrar nos dashboards."
  value       = random_password.admin_token.result
  sensitive   = true
}

output "license_key_secret_arn" {
  description = "ARN do secret da chave de licença (usado pelo script de migração)."
  value       = aws_secretsmanager_secret.license_key.arn
}
