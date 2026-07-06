<#
=============================================================================
 WorkPulse - Redeploy do backend + Publicar o site (PowerShell)
=============================================================================
 Atalho que roda, em sequencia:
   1) scripts\redeploy.ps1       (atualiza o servidor / backend na AWS)
   2) scripts\publicar-site.ps1  (publica site/ no S3 + limpa CloudFront)

 Rode 1x quando alterar BACKEND e FRONTEND ao mesmo tempo.
 Se algum passo falhar, o proximo NAO roda (para nao publicar pela metade).

 PRE-REQUISITOS: os mesmos dos dois scripts (AWS CLI v2 + 'aws configure').

 USO:
   cd "C:\Users\caiob\OneDrive\Documentos\WORK PULSE\workpulse-deploy"
   powershell -ExecutionPolicy Bypass -File scripts\redeploy-publicar.ps1
=============================================================================
#>
$ErrorActionPreference = "Stop"

# Pasta onde este script esta (resolve caminhos mesmo chamando de outro diretorio)
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$psExe     = (Get-Process -Id $PID).Path   # mesmo executavel de PowerShell em uso

function Invoke-Step {
    param([string]$Name, [string]$File)

    Write-Host ""
    Write-Host "==========================================================" -ForegroundColor Cyan
    Write-Host " $Name" -ForegroundColor Cyan
    Write-Host "==========================================================" -ForegroundColor Cyan

    if (-not (Test-Path $File)) {
        Write-Host "ERRO: nao encontrei $File" -ForegroundColor Red
        exit 1
    }

    # Roda cada script num processo filho para que um 'exit' interno
    # nao mate este wrapper; checamos o codigo de saida.
    & $psExe -ExecutionPolicy Bypass -File $File
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "FALHOU: '$Name' retornou codigo $LASTEXITCODE. Abortando." -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

$swTotal = [System.Diagnostics.Stopwatch]::StartNew()

Invoke-Step -Name "1/2  Redeploy do backend" -File (Join-Path $scriptDir "redeploy.ps1")
Invoke-Step -Name "2/2  Publicar o site"     -File (Join-Path $scriptDir "publicar-site.ps1")

$swTotal.Stop()
Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host " CONCLUIDO: backend + site atualizados em $([math]::Round($swTotal.Elapsed.TotalMinutes,1)) min." -ForegroundColor Green
Write-Host " Dica: no navegador, use Ctrl+Shift+R para limpar o cache." -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
