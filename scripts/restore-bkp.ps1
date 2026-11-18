<#
=============================================================================
 WorkPulse - Restaurar backup de deploy (rollback) (PowerShell)
=============================================================================
 Volta o que esta NO AR para um backup feito pelo redeploy-publicar-com-bkp.ps1.
   - BACKEND : aponta a tag 'latest' do ECR para a imagem do backup e reinicia
               o servico no ECS (sem CodeBuild, ~3-5 min).
   - SITE    : reenvia ao S3 a copia do site do backup (--delete) e limpa o
               CloudFront (~1 min).
 NAO mexe nos seus arquivos locais (server/, site/) - o codigo novo continua
 na pasta para corrigir e publicar de novo.
 NAO desfaz dados: colunas novas no banco ficam (inofensivas) e dados apagados
 pelo sistema (ex.: "liberar para estoque") nao voltam.

 PARAMETROS:
   -Backup 20261118-123000   qual backup (padrao: o ultimo, _deploy_bkps\ULTIMO.txt)
   -Only all|backend|site    o que restaurar (padrao: all)
   -Force                    nao pede confirmacao
   -List                     so lista os backups disponiveis

 USO:
   cd "C:\Users\caiob\OneDrive\Documentos\WORK PULSE\workpulse-deploy"
   powershell -ExecutionPolicy Bypass -File scripts\restore-bkp.ps1
   powershell -ExecutionPolicy Bypass -File scripts\restore-bkp.ps1 -Only site
   powershell -ExecutionPolicy Bypass -File scripts\restore-bkp.ps1 -List
=============================================================================
#>
param(
  [string]$Backup = "",
  [ValidateSet("all","backend","site")][string]$Only = "all",
  [switch]$Force,
  [switch]$List
)
$ErrorActionPreference = "Stop"

$Region  = "us-east-1"
$EcrRepo = "workpulse-prod-server"
$Cluster = "workpulse-prod"
$Service = "workpulse-prod-server"
$Domain  = "workpulse.com.br"
$env:AWS_DEFAULT_REGION = $Region

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$KitRoot   = Split-Path $scriptDir -Parent
$WpRoot    = Split-Path $KitRoot -Parent
$BkpRoot   = Join-Path $WpRoot "_deploy_bkps"

if (-not (Test-Path $BkpRoot)) { Write-Host "Nenhum backup encontrado em $BkpRoot" -ForegroundColor Red; exit 1 }

if ($List) {
  Write-Host "Backups em $BkpRoot :"
  Get-ChildItem $BkpRoot -Directory | Sort-Object Name -Descending | ForEach-Object { Write-Host "  $($_.Name)" }
  exit 0
}

if (-not $Backup) {
  $ultimo = Join-Path $BkpRoot "ULTIMO.txt"
  if (-not (Test-Path $ultimo)) { Write-Host "ULTIMO.txt nao existe; informe -Backup." -ForegroundColor Red; exit 1 }
  $Backup = (Get-Content $ultimo -Raw).Trim()
}
$BkpDir = Join-Path $BkpRoot $Backup
if (-not (Test-Path $BkpDir)) { Write-Host "Backup '$Backup' nao encontrado em $BkpRoot" -ForegroundColor Red; exit 1 }

$doBackend = ($Only -eq "all" -or $Only -eq "backend")
$doSite    = ($Only -eq "all" -or $Only -eq "site")

Write-Host "============================================================" -ForegroundColor Yellow
Write-Host " WorkPulse - ROLLBACK para o backup $Backup  (restaurar: $Only)" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Yellow
if (Test-Path (Join-Path $BkpDir "bkp-info.txt")) { Get-Content (Join-Path $BkpDir "bkp-info.txt") | ForEach-Object { Write-Host "  $_" } }

if (-not $Force) {
  $ans = Read-Host "`nConfirma o rollback? Digite SIM"
  if ($ans.Trim().ToUpper() -ne "SIM") { Write-Host "Cancelado."; exit 0 }
}

$sw = [System.Diagnostics.Stopwatch]::StartNew()

# --- Backend ----------------------------------------------------------------
if ($doBackend) {
  $manFile = Join-Path $BkpDir "ecr-manifest.json"
  $mtFile  = Join-Path $BkpDir "ecr-mediatype.txt"
  if (-not (Test-Path $manFile)) { Write-Host "ERRO: $manFile nao existe." -ForegroundColor Red; exit 1 }
  $mtype = if (Test-Path $mtFile) { (Get-Content $mtFile -Raw).Trim() } else { "" }

  Write-Host "`n>> Backend: apontando 'latest' para a imagem do backup..."
  $putArgs = @("ecr","put-image","--repository-name",$EcrRepo,"--image-tag","latest",
               "--image-manifest",("file://" + $manFile))
  if ($mtype) { $putArgs += @("--image-manifest-media-type",$mtype) }
  # PS 5.1: com EAP=Stop, '2>&1' em comando nativo aborta o script -> Continue so aqui.
  $ErrorActionPreference = "Continue"
  $out = & aws @putArgs 2>&1 | Out-String
  $rcPut = $LASTEXITCODE
  $ErrorActionPreference = "Stop"
  if ($rcPut -ne 0) {
    if ($out -match "ImageAlreadyExists") {
      Write-Host "  - 'latest' ja aponta para essa imagem (ok, segue)."
    } else {
      Write-Host "ERRO no ECR put-image:`n$out" -ForegroundColor Red
      Write-Host "Se a imagem expirou (lifecycle: 15 ultimas), restaure o main.py e rode redeploy.ps1." -ForegroundColor Red
      exit 1
    }
  }
  Write-Host ">> Backend: reiniciando o servico no ECS..."
  aws ecs update-service --cluster $Cluster --service $Service --force-new-deployment | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Host "ERRO no ecs update-service." -ForegroundColor Red; exit 1 }
  Write-Host "   aguardando estabilizar (alguns minutos)..."
  aws ecs wait services-stable --cluster $Cluster --services $Service
  if ($LASTEXITCODE -ne 0) { Write-Host "AVISO: o 'wait' expirou; confira o servico no console do ECS." -ForegroundColor Yellow }
  else { Write-Host "  - Backend restaurado." -ForegroundColor Green }
}

# --- Site -------------------------------------------------------------------
if ($doSite) {
  $SiteBkp = Join-Path $BkpDir "site"
  if (-not (Test-Path $SiteBkp) -or -not (Get-ChildItem -Recurse -File $SiteBkp | Select-Object -First 1)) {
    Write-Host "ERRO: copia do site vazia/inexistente em $SiteBkp" -ForegroundColor Red; exit 1
  }
  $AccountId  = (aws sts get-caller-identity --query Account --output text).Trim()
  $SiteBucket = "workpulse-site-$AccountId"
  Write-Host "`n>> Site: reenviando a copia do backup para s3://$SiteBucket ..."
  aws s3 sync "$SiteBkp/" "s3://$SiteBucket/" --delete --cache-control "public, max-age=3600" --only-show-errors
  if ($LASTEXITCODE -ne 0) { Write-Host "ERRO no s3 sync." -ForegroundColor Red; exit 1 }
  $DistId = (aws cloudfront list-distributions `
    --query "DistributionList.Items[?contains(Aliases.Items, '$Domain')].Id | [0]" --output text).Trim()
  if ([string]::IsNullOrEmpty($DistId) -or $DistId -eq "None") {
    Write-Host "AVISO: CloudFront nao encontrado; cache nao foi limpo." -ForegroundColor Yellow
  } else {
    aws cloudfront create-invalidation --distribution-id $DistId --paths "/*" | Out-Null
    Write-Host "  - Site restaurado e cache limpo ($DistId)." -ForegroundColor Green
  }
}

$sw.Stop()
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " ROLLBACK concluido em $([math]::Round($sw.Elapsed.TotalMinutes,1)) min. Use Ctrl+Shift+R no navegador." -ForegroundColor Green
Write-Host " Seus arquivos locais NAO foram alterados." -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
