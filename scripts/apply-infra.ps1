<#
=============================================================================
 WorkPulse - Aplicar mudancas de INFRA (Terraform) - PowerShell
=============================================================================
 Use quando a mudanca exigir recursos novos na AWS (ex.: a bucket de
 releases + permissao IAM da Fase 1a). NAO e necessario para deploys de
 codigo do servidor (esses sao o redeploy.ps1) nem do site (publicar-site.ps1).

 O estado do Terraform fica no S3 (backend remoto) + lock no DynamoDB, entao
 rodar daqui e seguro: o init baixa o estado atual e o apply mostra so o diff.

 PRE-REQUISITOS (uma vez):
   - AWS CLI v2 + 'aws configure' (mesma conta dos outros scripts)
   - Terraform instalado:
       winget install --id Hashicorp.Terraform -e
     (ou baixe em https://developer.hashicorp.com/terraform/install)

 USO:
   cd "C:\Users\caiob\OneDrive\Documentos\WORK PULSE\workpulse-deploy"
   powershell -ExecutionPolicy Bypass -File scripts\apply-infra.ps1
=============================================================================
#>
$ErrorActionPreference = "Stop"

$Region    = "us-east-1"
$LockTable = "workpulse-tf-locks"
$env:AWS_DEFAULT_REGION = $Region

Write-Host "============================================================"
Write-Host " WorkPulse - Aplicar infra (Terraform)"
Write-Host "============================================================"

# --- Confere o Terraform ----------------------------------------------------
if (-not (Get-Command terraform -ErrorAction SilentlyContinue)) {
  Write-Error "Terraform nao encontrado. Instale com:`n  winget install --id Hashicorp.Terraform -e`nDepois feche e reabra o PowerShell."
  exit 1
}
Write-Host ("  - " + (terraform version | Select-Object -First 1))

# --- Conta AWS / bucket de estado ------------------------------------------
$AccountId = (aws sts get-caller-identity --query Account --output text).Trim()
if (-not $AccountId) { Write-Error "Nao obtive o Account ID. Rode 'aws configure' primeiro."; exit 1 }
$StateBucket = "workpulse-tfstate-$AccountId"
Write-Host "  - Conta AWS: $AccountId"
Write-Host "  - Estado:    s3://$StateBucket/prod/terraform.tfstate"

$KitRoot  = Split-Path $PSScriptRoot -Parent
$InfraDir = Join-Path $KitRoot "infra"

Push-Location $InfraDir
try {
  # --- init (baixa o estado remoto) -----------------------------------------
  Write-Host "`n>> terraform init..."
  terraform init -input=false -reconfigure `
    -backend-config="bucket=$StateBucket" `
    -backend-config="key=prod/terraform.tfstate" `
    -backend-config="region=$Region" `
    -backend-config="dynamodb_table=$LockTable" `
    -backend-config="encrypt=true"
  if ($LASTEXITCODE -ne 0) { throw "terraform init falhou" }

  # --- plan (mostra o diff) -------------------------------------------------
  Write-Host "`n>> terraform plan..."
  terraform plan -input=false -out=tfplan
  if ($LASTEXITCODE -ne 0) { throw "terraform plan falhou" }

  Write-Host "`n------------------------------------------------------------"
  Write-Host " REVISE o plano acima. O esperado para a Fase 1a e:"
  Write-Host "   + aws_s3_bucket.releases                 (create)"
  Write-Host "   + aws_s3_bucket_public_access_block...   (create)"
  Write-Host "   + aws_s3_bucket_versioning.releases      (create)"
  Write-Host "   + aws_iam_role_policy.ecs_task_releases  (create)"
  Write-Host "   ~ aws_ecs_task_definition.server         (nova revisao: env nova)"
  Write-Host "   ~ aws_ecs_service.server                 (update)"
  Write-Host " Se aparecer qualquer DESTROY de RDS/EFS/rede/banco -> PARE (Ctrl+C)."
  Write-Host "------------------------------------------------------------"
  $ans = Read-Host "Aplicar este plano? digite 'sim' para confirmar"
  if ($ans -ne "sim") { Write-Host "Cancelado. Nada foi aplicado."; exit 0 }

  # --- apply (aplica exatamente o que foi revisado) -------------------------
  Write-Host "`n>> terraform apply..."
  terraform apply -input=false tfplan
  if ($LASTEXITCODE -ne 0) { throw "terraform apply falhou" }

  Remove-Item -Force tfplan -ErrorAction SilentlyContinue
  Write-Host "`n============================================================"
  Write-Host " Infra aplicada. Proximos passos:"
  Write-Host "   1) scripts\redeploy.ps1        (backend com os endpoints)"
  Write-Host "   2) scripts\publicar-site.ps1   (pagina do Platform Admin)"
  Write-Host "============================================================"
}
finally {
  Pop-Location
}
