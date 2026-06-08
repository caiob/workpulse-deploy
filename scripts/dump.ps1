$Base   = "https://api.workpulse.com.br"
$Tenant = "0292843554376"
$Token  = Read-Host "Cole o X-Admin-Token (master)"
$H = @{ "X-Admin-Token" = $Token }

# 1) Descobrir o user_id do Leandro
$users = Invoke-RestMethod -Uri "$Base/v1/users?tenant=$Tenant" -Headers $H
$leo = $users.users | Where-Object { $_.display_name -like "*Leandro*" }
$uid = $leo.user_id
"Leandro user_id: $uid"
$uidEnc = [uri]::EscapeDataString($uid)

# Janela ampla (UTC) cobrindo o dia 07/06 local
$since = "2026-06-06T00:00:00Z"; $until = "2026-06-09T00:00:00Z"

# 2) Eventos AFK (o suspeito) — ts + duração em horas + status
$afk = Invoke-RestMethod -Uri "$Base/v1/events?tenant=$Tenant&user_id=$uidEnc&bucket=afk&since=$since&until=$until&limit=1000&order=asc" -Headers $H
"== AFK events (total $($afk.total)) =="
$afk.events | ForEach-Object {
  [pscustomobject]@{ ts=$_.ts; dur_h=[math]::Round($_.duration_sec/3600,2); status=$_.data.status; device=$_.device_id }
} | Format-Table -Auto

# Soma das durações AFK (deve reproduzir o ~24h)
$somaH = [math]::Round((($afk.events | Measure-Object duration_sec -Sum).Sum)/3600,2)
"Soma AFK no período: $somaH h"

# 3) Janela (window): span do dia e contagem
$win = Invoke-RestMethod -Uri "$Base/v1/events?tenant=$Tenant&user_id=$uidEnc&bucket=window&since=$since&until=$until&limit=1000&order=asc" -Headers $H
"== Window: total $($win.total), mostrados $($win.events.Count) =="
if ($win.events.Count) {
  "  primeiro: $($win.events[0].ts)  dur=$($win.events[0].duration_sec)s"
  "  ultimo:   $($win.events[-1].ts) dur=$($win.events[-1].duration_sec)s"
}