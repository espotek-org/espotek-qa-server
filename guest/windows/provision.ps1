# Golden-image provisioning, run over ssh (elevated) by `qa vm build windows`
# after qa-firstlogon.ps1 has brought sshd up. Files it needs are copied to
# C:\qa first: qa-job.ps1 and mesa\*.dll.
param([Parameter(Mandatory = $true)][string]$Password)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

Write-Output 'autologon: keep logging qa in on every boot'
$wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty $wl -Name AutoAdminLogon -Value '1'
Set-ItemProperty $wl -Name DefaultUserName -Value 'qa'
Set-ItemProperty $wl -Name DefaultDomainName -Value '.'
Set-ItemProperty $wl -Name DefaultPassword -Value $Password
if (Get-ItemProperty $wl -Name AutoLogonCount -ErrorAction Ignore) { Remove-ItemProperty $wl -Name AutoLogonCount }

Write-Output 'updates: none in the background (runs must be reproducible; update the golden image deliberately)'
$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
New-Item -Path $au -Force | Out-Null
Set-ItemProperty $au -Name NoAutoUpdate -Type DWord -Value 1

Write-Output 'power: never sleep, blank or lock'
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off
$pers = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization'
New-Item -Path $pers -Force | Out-Null
Set-ItemProperty $pers -Name NoLockScreen -Type DWord -Value 1
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name ScreenSaveActive -Value '0'

Write-Output 'desktop: no "finish setting up your device" screen over the test windows'
$eng = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement'
New-Item -Path $eng -Force | Out-Null
Set-ItemProperty $eng -Name ScoobeSystemSettingEnabled -Type DWord -Value 0

# Defender stays on, as on users' machines.

Write-Output 'scheduled tasks that run jobs inside the logged-on desktop session'
foreach ($t in @(@{Name = 'QA-User'; Level = 'Limited'; Job = 'C:\qa\job-user.json'},
                 @{Name = 'QA-Elevated'; Level = 'Highest'; Job = 'C:\qa\job-elevated.json'})) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File C:\qa\qa-job.ps1 -Job $($t.Job)"
    $principal = New-ScheduledTaskPrincipal -UserId 'qa' -LogonType Interactive -RunLevel $t.Level
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 6) -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $t.Name -Action $action -Principal $principal -Settings $settings -Force | Out-Null
}
Write-Output 'provisioned'
