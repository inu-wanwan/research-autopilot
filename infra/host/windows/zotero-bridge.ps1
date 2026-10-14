# Windows side of the Eagle Zotero bridge (docs/operations/eagle_wsl_setup.md, Zotero). Run once in an
# elevated PowerShell. Idempotent: re-running replaces the same portproxy entry and firewall rule.
#   WSL (ra) 127.0.0.1:23119 -> relay -> <WSL host address>:23120 -> portproxy -> 127.0.0.1:23119 (Zotero)
# The firewall rule admits port 23120 only on the WSL virtual interface (never LAN / Tailscale).
$ErrorActionPreference = 'Stop'
$ListenPort = 23120
$ZoteroPort = 23119
$RuleName = 'Research Autopilot Zotero bridge (WSL only)'
$WslAlias = (Get-NetAdapter -IncludeHidden | Where-Object { $_.Name -like 'vEthernet (WSL*' } | Select-Object -First 1).Name
if (-not $WslAlias) { throw 'WSL virtual interface not found: start WSL first (open an Ubuntu terminal) and run again.' }

# portproxy needs the IP Helper service (on by default).
Set-Service iphlpsvc -StartupType Automatic
Start-Service iphlpsvc

# The delete fails harmlessly on the first run (no entry yet); keep native stderr from stopping the script.
$ErrorActionPreference = 'Continue'
netsh interface portproxy delete v4tov4 listenport=$ListenPort listenaddress=0.0.0.0 *> $null
$ErrorActionPreference = 'Stop'
netsh interface portproxy add v4tov4 listenport=$ListenPort listenaddress=0.0.0.0 connectport=$ZoteroPort connectaddress=127.0.0.1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "netsh portproxy add failed ($LASTEXITCODE)" }

Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $ListenPort `
  -InterfaceAlias $WslAlias -Profile Any | Out-Null

Write-Host "portproxy:"; netsh interface portproxy show v4tov4
Write-Host "firewall: $RuleName on '$WslAlias' (TCP $ListenPort)"
