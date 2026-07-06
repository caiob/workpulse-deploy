<#
=============================================================================
 WorkPulse - Dump dos eventos crus de um colaborador (diagnostico)
=============================================================================
 O RDS e privado (sem acesso externo), entao o dump e feito PELA API:
 chama /v1/admin/events-dump (Platform Admin) e salva o JSON num arquivo.
 Voce zipa esse arquivo e envia para analise.

 PRE-REQUISITO: o backend com o endpoint precisa estar no ar (redeploy.ps1).

 USO (exemplos):
   powershell -ExecutionPolicy Bypass -File scripts\dump-user.ps1
   powershell -ExecutionPolicy Bypass -File scripts\dump-user.ps1 -Match "Caio Batalha" -Days 2
   powershell -ExecutionPolicy Bypass -File scripts\dump-user.ps1 -Bucket afk
=============================================================================
#>
param(
    [string]$ApiUrl     = "https://api.workpulse.com.br",
    [string]$AdminToken = "",                       # se vazio, pergunta
    [string]$Tenant     = "0292843554376",          # tenant ASAPTECH
    [string]$Match      = "asaptech-note-vta",       # nome, user_id OU hostname (parcial)
    [int]   $Days       = 2,                          # quantos dias para tras
    [string]$Bucket     = "afk"                          # ""=todos | "afk" | "window"
)
$ErrorActionPreference = "Stop"

if (-not $AdminToken) { $AdminToken = Read-Host "Token Admin (X-Admin-Token)" }

$until = (Get-Date).ToUniversalTime().ToString("o")
$since = (Get-Date).ToUniversalTime().AddDays(-$Days).ToString("o")

$qs = "tenant=$([uri]::EscapeDataString($Tenant))" +
      "&match=$([uri]::EscapeDataString($Match))" +
      "&since=$([uri]::EscapeDataString($since))" +
      "&until=$([uri]::EscapeDataString($until))"
if ($Bucket) { $qs += "&bucket=$([uri]::EscapeDataString($Bucket))" }

$url = "$ApiUrl/v1/admin/events-dump?$qs"
Write-Host "============================================================"
Write-Host " Dump de '$Match'  (ultimos $Days dias$( if ($Bucket) { " - bucket=$Bucket" } ))"
Write-Host "============================================================"
Write-Host ">> $url`n"

$r = Invoke-WebRequest -Method Get -Uri $url -Headers @{ "X-Admin-Token" = $AdminToken } `
        -UseBasicParsing -TimeoutSec 180
if ($r.StatusCode -ne 200) { Write-Error "HTTP $($r.StatusCode): $($r.Content)"; exit 1 }

$stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
$safe  = ($Match -replace '[^\w\-]', '_')
$out   = Join-Path (Get-Location) "dump-$safe-$stamp.json"
[System.IO.File]::WriteAllText($out, $r.Content, [System.Text.Encoding]::UTF8)

# Resumo leve
try {
    $j = $r.Content | ConvertFrom-Json
    Write-Host "Usuarios encontrados:"
    if ($j.users.Count -eq 0) { Write-Host "  (nenhum - confira o -Match / -Tenant)" }
    $j.users | ForEach-Object { Write-Host "  - $($_.user_id)   ($($_.display_name))" }
    Write-Host "Eventos no dump: $($j.count)"
} catch { Write-Host "(nao consegui resumir, mas o arquivo foi salvo)" }

$sizeKB = [math]::Round((Get-Item $out).Length / 1KB, 0)
Write-Host ""
Write-Host "Arquivo salvo: $out  ($sizeKB KB)"
Write-Host "Agora ZIPE esse .json e me envie."
Write-Host "============================================================"
