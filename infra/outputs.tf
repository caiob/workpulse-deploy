# =============================================================================
# WorkPulse — Saídas do Terraform
# =============================================================================
# Estes valores são exibidos ao final de `terraform apply` e usados pelos
# scripts de deploy.
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
  description = "Domínio do CloudFront (caso precise apontar o DNS manualmente)."
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
  description = "Nome do projeto CodeBuild que constrói a imagem."
  value       = aws_codebuild_project.server.name
}

output "site_bucket_name" {
  description = "Bucket S3 onde os arquivos do site são publicados."
  value       = aws_s3_bucket.site.bucket
}

output "ecs_cluster_name" {
  description = "Nome do cluster ECS."
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "Nome do serviço ECS do servidor."
  value       = aws_ecs_service.server.name
}

output "admin_token" {
  description = "Token de administração — necessário para entrar nos dashboards."
  value       = random_password.admin_token.result
  sensitive   = true
}
