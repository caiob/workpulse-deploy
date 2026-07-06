<#
=============================================================================
 WorkPulse - Relatorio de duplicacao / AFK por usuario (diagnostico de frota)
=============================================================================
 Mostra, por colaborador: total de eventos, duplicatas estimadas (% ), e o
 maior AFK. Serve p/ ver se a duplicacao/AFK gigante e da frota toda ou de
 uma maquina so - SEM baixar os eventos crus.

 PRE-REQUISITO: backend com o endpoint no ar (redeploy.ps1).

 USO:
   powershell -ExecutionPolicy Bypass -File scripts\dup-report.ps1
   powershell -ExecutionPolicy Bypass -File scripts\dup-report.ps1 -Days 2
=============================================================================
#>
param(
    [string]$ApiUrl     = "https://api.workpulse.com.br",
    [string]$AdminToken = "",                 # se vazio, pergunta (NAO deixe o token salvo aqui)
    [string]$Tenant     = "0292843554376",
    [int]   $Days       = 0                    # 0 = so hoje; N = ultimos N dias
)
$ErrorActionPreference = "Stop"
if (-not $AdminToken) { $AdminToken = Read-Host "Token Admin (X-Admin-Token)" }

$qs = "tenant=$([uri]::EscapeDataString($Tenant))"
if ($Days -gt 0) {
    $since = (Get-Date).ToUniversalTime().AddDays(-$Days).ToString("o")
    $qs += "&since=$([uri]::EscapeDataString($since))"
}

$url = "$ApiUrl/v1/admin/dup-report?$qs"
Write-Host "Buscando relatorio... ($url)`n"
$j = Invoke-RestMethod -Method Get -Uri $url -Headers @{ "X-Admin-Token" = $AdminToken } -TimeoutSec 180

if (-not $j.users -or $j.users.Count -eq 0) { Write-Host "Sem dados no periodo."; exit 0 }

# Tabela ordenada por % de duplicata
$j.users |
  Sort-Object dup_pct -Descending |
  Format-Table @{L="Colaborador";E={$_.name}},
               @{L="Total";E={$_.total}},
               @{L="AFK ev";E={$_.afk_events}},
               @{L="Dups";E={$_.dups}},
               @{L="Dup %";E={$_.dup_pct}},
               @{L="Maior AFK (h)";E={$_.max_afk_h}},
               @{L="AFK>1h";E={$_.afk_gt_1h}} -AutoSize

$tot   = ($j.users | Measure-Object total -Sum).Sum
$totd  = ($j.users | Measure-Object dups  -Sum).Sum
$comDup = ($j.users | Where-Object { $_.dups -gt 0 }).Count
Write-Host ("Periodo: {0} -> {1}" -f $j.since, $j.until)
Write-Host ("Usuarios: {0}  |  com duplicata: {1}  |  total eventos: {2}  |  dups: {3}" -f $j.users.Count, $comDup, $tot, $totd)

$out = Join-Path (Get-Location) ("dup-report-" + (Get-Date).ToString("yyyyMMdd-HHmmss") + ".json")
$j | ConvertTo-Json -Depth 6 | Out-File -FilePath $out -Encoding utf8
Write-Host "`nJSON salvo: $out  (me envie se quiser que eu analise)"
