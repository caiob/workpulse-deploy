<#
=============================================================================
 WorkPulse - Verificacao da Fase A (cap de AFK gigante na jornada)
=============================================================================
 Para cada colaborador, puxa os eventos CRUS (janela + AFK) via events-dump e
 calcula o "AFK na Jornada" por dia local de DUAS formas:
   - SEM cap  (comportamento antigo: conta todo AFK)
   - COM cap  (Fase A: descarta evento de AFK > WORKPULSE_AFK_MAX_SEC / 3h)
 Assim da pra ver, no mesmo dado, o efeito da Fase A (o painel mostra a versao COM cap).

 Logica = a mesma do backend (/v1/jornada):
   janela do dia = 1a coleta de tela -> ultima coleta de tela
   AFK na jornada = soma dos AFK cujo inicio cai DENTRO dessa janela

 USO:
   powershell -ExecutionPolicy Bypass -File scripts\afk-jornada-check.ps1
   powershell -ExecutionPolicy Bypass -File scripts\afk-jornada-check.ps1 -Users "Ravi Leal","Luan Nabarrete" -Days 1
=============================================================================
#>
param(
    [string]  $ApiUrl     = "https://api.workpulse.com.br",
    [string]  $AdminToken = "",                          # se vazio, pergunta
    [string]  $Tenant     = "0292843554376",             # ASAPTECH
    [string[]]$Users      = @("Ravi Leal","Luan Nabarrete","Rodrigo Almeida"),
    [int]     $Days       = 1,                            # dias para tras (1 = hoje)
    [int]     $CapSec     = 10800                         # teto = 3h (igual ao WORKPULSE_AFK_MAX_SEC)
)
$ErrorActionPreference = "Stop"
if (-not $AdminToken) { $AdminToken = Read-Host "Token Admin (X-Admin-Token)" }

$until = (Get-Date).ToUniversalTime().ToString("o")
$since = (Get-Date).ToUniversalTime().AddDays(-$Days).ToString("o")
$TZ    = -3   # America/Sao_Paulo (sem horario de verao)

function To-Local([string]$ts) {
    return [datetimeoffset]::Parse($ts).UtcDateTime.AddHours($TZ)
}

Write-Host "============================================================"
Write-Host " Fase A - AFK na Jornada: SEM cap vs COM cap ($([math]::Round($CapSec/3600,1))h)"
Write-Host " Periodo: ultimos $Days dia(s)"
Write-Host "============================================================`n"

foreach ($u in $Users) {
    $qs  = "tenant=$([uri]::EscapeDataString($Tenant))" +
           "&match=$([uri]::EscapeDataString($u))" +
           "&since=$([uri]::EscapeDataString($since))" +
           "&until=$([uri]::EscapeDataString($until))"   # SEM bucket = traz window + afk
    $url = "$ApiUrl/v1/admin/events-dump?$qs"
    try {
        $r = Invoke-RestMethod -Method Get -Uri $url -Headers @{ "X-Admin-Token" = $AdminToken } -TimeoutSec 180
    } catch {
        Write-Host ("[{0}] falha ao puxar: {1}" -f $u, $_.Exception.Message) -ForegroundColor Red
        continue
    }
    $ev = $r.events
    if (-not $ev -or $ev.Count -eq 0) { Write-Host ("[{0}] sem eventos no periodo" -f $u); continue }

    # Agrupa por dia local
    $byDay = @{}
    foreach ($e in $ev) {
        $lts = To-Local $e.ts
        $day = $lts.ToString("yyyy-MM-dd")
        if (-not $byDay.ContainsKey($day)) { $byDay[$day] = @{ win=@(); afk=@() } }
        if     ($e.bucket -eq 'window') { $byDay[$day].win += [pscustomobject]@{ s=$lts; e=$lts.AddSeconds([double]$e.duration_sec) } }
        elseif ($e.bucket -eq 'afk' -and $e.data_json.status -eq 'afk') { $byDay[$day].afk += [pscustomobject]@{ s=$lts; dur=[double]$e.duration_sec } }
    }

    Write-Host ("Colaborador: {0}" -f $u) -ForegroundColor Cyan
    Write-Host ("  {0,-12} {1,12} {2,12} {3,10}" -f "dia","AFK s/cap","AFK c/cap","descart.")
    $tNo=0.0; $tCap=0.0
    foreach ($day in ($byDay.Keys | Sort-Object)) {
        $wins = $byDay[$day].win
        if ($wins.Count -eq 0) { continue }   # sem jornada (sem tela) nesse dia
        $start = ($wins | Sort-Object s | Select-Object -First 1).s
        $end   = ($wins | Sort-Object e | Select-Object -Last  1).e
        $afkNo=0.0; $afkCap=0.0; $disc=0
        foreach ($a in $byDay[$day].afk) {
            if ($a.s -ge $start -and $a.s -le $end) {
                $afkNo += $a.dur
                if ($a.dur -le $CapSec) { $afkCap += $a.dur } else { $disc++ }
            }
        }
        $tNo += $afkNo; $tCap += $afkCap
        Write-Host ("  {0,-12} {1,10:N2}h {2,10:N2}h {3,10}" -f $day, ($afkNo/3600), ($afkCap/3600), $disc)
    }
    $red = if ($tNo -gt 0) { (1 - $tCap/$tNo) * 100 } else { 0 }
    Write-Host ("  {0,-12} {1,10:N2}h {2,10:N2}h   ({3:N0}% a menos)`n" -f "TOTAL", ($tNo/3600), ($tCap/3600), $red) -ForegroundColor Green
}
Write-Host "============================================================"
Write-Host "A coluna 'AFK c/cap' e o que o painel mostra apos a Fase A."
Write-Host "============================================================"
