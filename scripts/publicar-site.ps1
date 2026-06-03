<#
=============================================================================
 WorkPulse - Publicar o site e os dashboards (PowerShell)
=============================================================================
 Equivalente ao scripts/3-publicar-site.sh, para rodar no Windows PowerShell.
 Envia a pasta site/ para o S3 e limpa o cache do CloudFront.
 Rode sempre que alterar arquivos do site (HTML/CSS/JS).

 PRE-REQUISITOS:
   - AWS CLI v2 instalado:  https://aws.amazon.com/cli/
   - Credenciais configuradas (uma vez):  aws configure
     (Access Key / Secret da conta 881027528914, regiao us-east-1)

 USO:
   cd "C:\Users\caiob\Documents\WORK PULSE\workpulse-deploy"
   powershell -ExecutionPolicy Bypass -File scripts\publicar-site.ps1
=============================================================================
#>
$ErrorActionPreference = "Stop"

$Region = "us-east-1"
$Domain = "workpulse.com.br"
$env:AWS_DEFAULT_REGION = $Region

Write-Host "============================================================"
Write-Host " WorkPulse - Publicar o site"
Write-Host "============================================================"

$KitRoot = Split-Path $PSScriptRoot -Parent
$SiteDir = Join-Path $KitRoot "site"

$AccountId  = (aws sts get-caller-identity --query Account --output text).Trim()
if (-not $AccountId) { Write-Error "Nao foi possivel obter o Account ID. Rode 'aws configure' primeiro."; exit 1 }
$SiteBucket = "workpulse-site-$AccountId"

# --- Confere se o bucket existe --------------------------------------------
aws s3api head-bucket --bucket $SiteBucket 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Error "O bucket '$SiteBucket' nao existe. Rode a etapa 2 (infra) antes desta."
  exit 1
}

# --- Envia os arquivos -----------------------------------------------------
Write-Host "`n>> Enviando os arquivos do site para o S3..."
aws s3 sync "$SiteDir/" "s3://$SiteBucket/" --delete --cache-control "public, max-age=3600"
if ($LASTEXITCODE -ne 0) { Write-Error "Falha no s3 sync."; exit 1 }

# --- Invalida o cache do CloudFront ----------------------------------------
Write-Host "`n>> Limpando o cache do CloudFront..."
$DistId = (aws cloudfront list-distributions `
  --query "DistributionList.Items[?contains(Aliases.Items, '$Domain')].Id | [0]" `
  --output text).Trim()

if ([string]::IsNullOrEmpty($DistId) -or $DistId -eq "None") {
  Write-Host "AVISO: distribuicao CloudFront nao encontrada. O site foi enviado, mas o cache nao foi limpo."
} else {
  aws cloudfront create-invalidation --distribution-id $DistId --paths "/*" | Out-Null
  Write-Host "  - Cache limpo (distribuicao $DistId)."
}

Write-Host "`n============================================================"
Write-Host " Site publicado!"
Write-Host "   Site institucional : https://$Domain"
Write-Host "   Painel do cliente  : https://$Domain/dashboard"
Write-Host "   Console admin      : https://$Domain/adm"
Write-Host " (CloudFront pode levar alguns minutos para propagar.)"
Write-Host "============================================================"
