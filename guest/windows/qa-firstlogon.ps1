# Runs once at the qa account's first logon (from autounattend.xml): install
# OpenSSH from the config CD and authorise the host's key. Everything else is
# provisioned over ssh by `qa vm build windows`, where failures are visible.
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force C:\qa | Out-Null
Start-Transcript -Path C:\qa\firstlogon.log
$src = Split-Path -Parent $MyInvocation.MyCommand.Path

$msi = Get-ChildItem $src -Filter 'OpenSSH-Win64-*.msi' | Select-Object -First 1
$p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList "/i `"$($msi.FullName)`" /qn /norestart"
if ($p.ExitCode -ne 0) { throw "OpenSSH MSI exited $($p.ExitCode)" }

# qa is an administrator, so sshd reads keys from administrators_authorized_keys,
# which must be writable only by Administrators and SYSTEM.
$keys = 'C:\ProgramData\ssh\administrators_authorized_keys'
Copy-Item "$src\authorized_keys" $keys -Force
icacls $keys /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null

New-Item -Path HKLM:\SOFTWARE\OpenSSH -Force | Out-Null
New-ItemProperty -Path HKLM:\SOFTWARE\OpenSSH -Name DefaultShell -PropertyType String -Force `
  -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' | Out-Null
New-NetFirewallRule -Name qa-sshd -DisplayName 'QA sshd' -Direction Inbound -Protocol TCP -LocalPort 22 `
  -Action Allow -Profile Any | Out-Null
Set-Service sshd -StartupType Automatic
Restart-Service sshd
Stop-Transcript
