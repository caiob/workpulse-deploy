<#
 Dump dos eventos crus da Sara Salvador — dia local 06/07/2026 (America/Sao_Paulo).
 Janela UTC: 2026-07-06T03:00:00Z -> 2026-07-07T03:00:00Z. Todos os buckets.
 USO: powershell -ExecutionPolicy Bypass -File scripts\dump-sara-0607.ps1
#>
param(
    [string]$ApiUrl     = "https://api.workpulse.com.br",
    [string]$AdminToken = "",
    [string]$Tenant     = "0292843554376",   # ASAPTECH
    [string]$Match      = "Sara"
)
$ErrorActionPreference = "Stop"
if (-not $AdminToken) { $AdminToken = Read-Host "Token Admin (X-Admin-Token)" }

$since = "2026-07-06T03:00:00Z"
$until = "2026-07-07T03:00:00Z"
$qs = "tenant=$([uri]::EscapeDataString($Tenant))" +
      "&match=$([uri]::EscapeDataString($Match))" +
      "&since=$([uri]::EscapeDataString($since))" +
      "&until=$([uri]::EscapeDataString($until))"
# sem &bucket= -> traz TODOS (window + afk)

$url = "$ApiUrl/v1/admin/events-dump?$qs"
Write-Host ">> $url`n"
$r = Invoke-WebRequest -Method Get -Uri $url -Headers @{ "X-Admin-Token" = $AdminToken } `
        -UseBasicParsing -TimeoutSec 300
if ($r.StatusCode -ne 200) { Write-Error "HTTP $($r.StatusCode): $($r.Content)"; exit 1 }

$out = Join-Path (Split-Path $PSScriptRoot -Parent) "..\dump-sara-0607.json"
$out = [System.IO.Path]::GetFullPath($out)
[System.IO.File]::WriteAllText($out, $r.Content, [System.Text.Encoding]::UTF8)

$j = $r.Content | ConvertFrom-Json
Write-Host "Usuarios casados com '$Match':"
$j.users | ForEach-Object { Write-Host "  - $($_.user_id)  ($($_.display_name))" }
Write-Host "Eventos no dump: $($j.count)"
Write-Host "Salvo em: $out"
