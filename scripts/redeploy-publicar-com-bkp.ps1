<#
=============================================================================
 WorkPulse - Redeploy + Publicar COM BACKUP (PowerShell)
=============================================================================
 Antes de publicar, tira um "retrato" do que esta NO AR agora:
   - BACKEND : marca a imagem atual do ECR com a tag  bkp-<data-hora>
               e salva o manifesto em _deploy_bkps\<data-hora>\
   - SITE    : baixa o site publicado (bucket S3) para _deploy_bkps\<data-hora>\site\
 Depois roda o redeploy-publicar.ps1 normal (backend + site).

 Se algo der errado, volte com:
   powershell -ExecutionPolicy Bypass -File scripts\restore-bkp.ps1
   (sem parametros = restaura o ULTIMO backup; ver restore-bkp.ps1)

 Os backups ficam em  WORK PULSE\_deploy_bkps\  (FORA de site/ e do git).
 Obs.: o ECR guarda as ultimas 15 imagens; um backup muito antigo pode expirar.

 USO:
   cd "C:\Users\caiob\OneDrive\Documentos\WORK PULSE\workpulse-deploy"
   powershell -ExecutionPolicy Bypass -File scripts\redeploy-publicar-com-bkp.ps1
=============================================================================
#>
$ErrorActionPreference = "Stop"

$Region   = "us-east-1"
$EcrRepo  = "workpulse-prod-server"
$env:AWS_DEFAULT_REGION = $Region

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$KitRoot   = Split-Path $scriptDir -Parent          # workpulse-deploy
$WpRoot    = Split-Path $KitRoot -Parent            # WORK PULSE
$psExe     = (Get-Process -Id $PID).Path
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

$Stamp   = Get-Date -Format "yyyyMMdd-HHmmss"
$BkpRoot = Join-Path $WpRoot "_deploy_bkps"
$BkpDir  = Join-Path $BkpRoot $Stamp
New-Item -ItemType Directory -Force -Path (Join-Path $BkpDir "site") | Out-Null

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " WorkPulse - Backup do que esta no ar  ($Stamp)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$AccountId = (aws sts get-caller-identity --query Account --output text).Trim()
if (-not $AccountId) { Write-Host "ERRO: rode 'aws configure' primeiro." -ForegroundColor Red; exit 1 }
$SiteBucket = "workpulse-site-$AccountId"

# --- 1) Backend: tag da imagem que esta RODANDO + manifesto -----------------
# Usa o digest da task em execucao (nao a tag 'latest'): se um deploy anterior falhou e o
# ECS voltou sozinho, 'latest' aponta p/ uma imagem que NUNCA rodou (incidente 2026-11-18).
Write-Host "`n>> [1/2] Backend: guardando a imagem que esta rodando no ECS..."
$Cluster = "workpulse-prod"; $Service = "workpulse-prod-server"
$runDigest = ""
$taskArn = (aws ecs list-tasks --cluster $Cluster --service-name $Service --desired-status RUNNING --query "taskArns[0]" --output text).Trim()
if ($taskArn -and $taskArn -ne "None") {
  $runDigest = (aws ecs describe-tasks --cluster $Cluster --tasks $taskArn --query "tasks[0].containers[0].imageDigest" --output text).Trim()
}
if ($runDigest -and $runDigest -like "sha256:*") {
  Write-Host "  - Task rodando usa $runDigest"
  $imgId = "imageDigest=$runDigest"
} else {
  Write-Host "  - AVISO: nao achei a task rodando; usando a tag 'latest'." -ForegroundColor Yellow
  $imgId = "imageTag=latest"
}
$imgJson = aws ecr batch-get-image --repository-name $EcrRepo --image-ids $imgId --query "images[0]" --output json | Out-String
if ($LASTEXITCODE -ne 0 -or -not $imgJson.Trim() -or $imgJson.Trim() -eq "null") {
  Write-Host "ERRO: nao consegui ler a imagem ($imgId) do ECR ($EcrRepo)." -ForegroundColor Red; exit 1
}
$img      = $imgJson | ConvertFrom-Json
$manifest = $img.imageManifest
$mtype    = $img.imageManifestMediaType
$digest   = $img.imageId.imageDigest

[IO.File]::WriteAllText((Join-Path $BkpDir "ecr-manifest.json"), $manifest, $Utf8NoBom)
[IO.File]::WriteAllText((Join-Path $BkpDir "ecr-mediatype.txt"), "$mtype", $Utf8NoBom)

$BkpTag = "bkp-$Stamp"
$putArgs = @("ecr","put-image","--repository-name",$EcrRepo,"--image-tag",$BkpTag,
             "--image-manifest",("file://" + (Join-Path $BkpDir "ecr-manifest.json")))
if ($mtype) { $putArgs += @("--image-manifest-media-type",$mtype) }
# PS 5.1: com EAP=Stop, '2>&1' em comando nativo aborta o script -> Continue so aqui.
$ErrorActionPreference = "Continue"
$out = & aws @putArgs 2>&1 | Out-String
$rcPut = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($rcPut -ne 0) {
  Write-Host "ERRO ao criar a tag $BkpTag no ECR:`n$out" -ForegroundColor Red; exit 1
}
Write-Host "  - Imagem $digest marcada como '$BkpTag'."

# --- 2) Site: copia o que esta publicado no S3 ------------------------------
Write-Host "`n>> [2/2] Site: baixando o que esta publicado em s3://$SiteBucket ..."
aws s3 sync "s3://$SiteBucket/" (Join-Path $BkpDir "site") --only-show-errors
if ($LASTEXITCODE -ne 0) { Write-Host "ERRO ao baixar o site do S3." -ForegroundColor Red; exit 1 }
$nFiles = (Get-ChildItem -Recurse -File (Join-Path $BkpDir "site")).Count
Write-Host "  - $nFiles arquivos salvos."

# --- Registro do backup -----------------------------------------------------
$info = @"
backup      : $Stamp
ecr_repo    : $EcrRepo
ecr_tag     : $BkpTag
ecr_digest  : $digest
site_bucket : $SiteBucket
site_files  : $nFiles
"@
[IO.File]::WriteAllText((Join-Path $BkpDir "bkp-info.txt"), $info, $Utf8NoBom)
[IO.File]::WriteAllText((Join-Path $BkpRoot "ULTIMO.txt"), $Stamp, $Utf8NoBom)
Write-Host "`n  Backup OK em: $BkpDir" -ForegroundColor Green

# --- Deploy normal ------------------------------------------------------------
& $psExe -ExecutionPolicy Bypass -File (Join-Path $scriptDir "redeploy-publicar.ps1")
$rc = $LASTEXITCODE
if ($rc -ne 0) {
  Write-Host ""
  Write-Host "O deploy FALHOU (codigo $rc). Para voltar ao estado anterior:" -ForegroundColor Yellow
  Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\restore-bkp.ps1 -Backup $Stamp" -ForegroundColor Yellow
  exit $rc
}
Write-Host ""
Write-Host "Se algo estiver errado no ar, rollback com:" -ForegroundColor Yellow
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\restore-bkp.ps1 -Backup $Stamp" -ForegroundColor Yellow
