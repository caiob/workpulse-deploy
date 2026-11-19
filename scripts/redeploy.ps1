<#
=============================================================================
 WorkPulse - Atualizar o servidor / backend (PowerShell)
=============================================================================
 Equivalente ao scripts/redeploy.sh, para rodar no Windows PowerShell.
 NAO builda Docker localmente: empacota o codigo do server/, envia ao S3,
 dispara o CodeBuild (build na AWS) e forca o ECS a subir a nova imagem
 (rolling deploy, zero downtime).
 Rode quando alterar server/main.py (ou migration/requirements/Dockerfile).

 PRE-REQUISITOS:
   - AWS CLI v2 instalado:  https://aws.amazon.com/cli/
   - Credenciais configuradas (uma vez):  aws configure
     (conta 881027528914, regiao us-east-1)
   - NAO precisa de Docker (o build roda no AWS CodeBuild).

 USO:
   cd "C:\Users\caiob\Documents\WORK PULSE\workpulse-deploy"
   powershell -ExecutionPolicy Bypass -File scripts\redeploy.ps1
=============================================================================
#>
$ErrorActionPreference = "Stop"

$Region           = "us-east-1"
$CodeBuildProject = "workpulse-prod-image-build"
$Cluster          = "workpulse-prod"
$Service          = "workpulse-prod-server"
$env:AWS_DEFAULT_REGION = $Region

Write-Host "============================================================"
Write-Host " WorkPulse - Atualizar o servidor (zero downtime)"
Write-Host "============================================================"

$KitRoot   = Split-Path $PSScriptRoot -Parent
$ServerDir = Join-Path $KitRoot "server"

$AccountId    = (aws sts get-caller-identity --query Account --output text).Trim()
if (-not $AccountId) { Write-Error "Nao foi possivel obter o Account ID. Rode 'aws configure' primeiro."; exit 1 }
$DeployBucket = "workpulse-deploy-$AccountId"

# --- Empacota o codigo do servidor (zip plano, arquivos na raiz) ------------
Write-Host "`n>> Empacotando e enviando o codigo do servidor..."
$Zip = Join-Path $env:TEMP "server-src.zip"
if (Test-Path $Zip) { Remove-Item $Zip -Force }
$Files = @("main.py","behavior_analysis.py","migration.py","requirements.txt","Dockerfile") |
         ForEach-Object { Join-Path $ServerDir $_ }
foreach ($f in $Files) { if (-not (Test-Path $f)) { Write-Error "Arquivo nao encontrado: $f"; exit 1 } }
Compress-Archive -Path $Files -DestinationPath $Zip -Force

aws s3 cp $Zip "s3://$DeployBucket/server-src.zip"
if ($LASTEXITCODE -ne 0) { Write-Error "Falha ao enviar o zip ao S3."; exit 1 }

# --- Dispara o CodeBuild e aguarda -----------------------------------------
Write-Host "`n>> Reconstruindo a imagem no AWS CodeBuild..."
$BuildId = (aws codebuild start-build --project-name $CodeBuildProject `
  --query 'build.id' --output text).Trim()
Write-Host "  - Build: $BuildId (aguardando...)"

do {
  Start-Sleep -Seconds 15
  Write-Host "." -NoNewline
  $Status = (aws codebuild batch-get-builds --ids $BuildId `
    --query 'builds[0].buildStatus' --output text).Trim()
} while ($Status -eq "IN_PROGRESS")
Write-Host ""

if ($Status -ne "SUCCEEDED") {
  Write-Error "O build falhou (status: $Status). Veja os logs no console do CodeBuild."
  exit 1
}
Write-Host "  - Nova imagem publicada."

# --- Forca o ECS a usar a nova imagem (rolling deploy) ---------------------
Write-Host "`n>> Atualizando o servico ECS (rolling deployment, sem downtime)..."
aws ecs update-service --cluster $Cluster --service $Service --force-new-deployment | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "Falha ao atualizar o servico ECS."; exit 1 }

Write-Host "  - Aguardando estabilizacao..."
aws ecs wait services-stable --cluster $Cluster --services $Service
$waitRc = $LASTEXITCODE

# --- Confere se a task RODANDO usa a imagem nova -----------------------------
# 'services-stable' tambem fica OK quando o circuit breaker VOLTA p/ a versao antiga
# (incidente 2026-11-18: "sucesso" com a imagem antiga no ar). Compara os digests.
$newDigest = (aws ecr describe-images --repository-name "workpulse-prod-server" --image-ids imageTag=latest `
  --query "imageDetails[0].imageDigest" --output text).Trim()
$taskArn = (aws ecs list-tasks --cluster $Cluster --service-name $Service --desired-status RUNNING `
  --query "taskArns[0]" --output text).Trim()
$runDigest = ""
if ($taskArn -and $taskArn -ne "None") {
  $runDigest = (aws ecs describe-tasks --cluster $Cluster --tasks $taskArn `
    --query "tasks[0].containers[0].imageDigest" --output text).Trim()
}
if ($runDigest -ne $newDigest) {
  Write-Host ""
  Write-Host "============================================================" -ForegroundColor Red
  Write-Host " FALHOU: o ECS NAO esta rodando a imagem nova." -ForegroundColor Red
  Write-Host "   nova    : $newDigest" -ForegroundColor Red
  Write-Host "   rodando : $runDigest" -ForegroundColor Red
  Write-Host " Provavel rollback automatico (circuit breaker) ou wait expirado (rc=$waitRc)." -ForegroundColor Red
  Write-Host " Veja: aws ecs describe-services --cluster $Cluster --services $Service --query 'services[0].events[0:15].message'" -ForegroundColor Red
  Write-Host "============================================================" -ForegroundColor Red
  exit 2
}
Write-Host "  - Confirmado: task rodando com a imagem nova ($runDigest)."

Write-Host "`n============================================================"
Write-Host " Servidor atualizado com sucesso (sem downtime)."
Write-Host "============================================================"
