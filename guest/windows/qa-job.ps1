# Runs one job inside the interactive desktop session (started by the QA-User
# or QA-Elevated scheduled task). The host writes the job file over ssh:
#   {"argv": [...], "cwd": "...", "log": "...", "exit_file": "..."}
# stdout and stderr go to the log together; the exit code is written
# atomically to exit_file when the process ends.
param([Parameter(Mandatory = $true)][string]$Job)
$ErrorActionPreference = 'Stop'
$j = Get-Content $Job -Raw | ConvertFrom-Json
foreach ($a in $j.argv) {
    if ($a -match '["%^&|<>]') { throw "argument not safe for cmd.exe quoting: $a" }
}
$cmdline = (($j.argv | ForEach-Object { '"' + $_ + '"' }) -join ' ') + ' > "' + $j.log + '" 2>&1'
try {
    $p = Start-Process -FilePath cmd.exe -ArgumentList @('/d', '/s', '/c', '"' + $cmdline + '"') `
        -WorkingDirectory $j.cwd -WindowStyle Minimized -Wait -PassThru
    $code = $p.ExitCode
} catch {
    Add-Content -Path $j.log -Value "qa-job: could not start the job: $_"
    $code = 9009
}
Set-Content -Path ($j.exit_file + '.tmp') -Value $code -Encoding ascii
Move-Item -Force ($j.exit_file + '.tmp') $j.exit_file
