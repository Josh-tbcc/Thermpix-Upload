# Sets up Thermpix DPA Sync: installs what it needs, saves your login and
# schedules it to run every day at 7pm. Safe to run again to update or repair.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $here ".venv"
$taskName = "Thermpix DPA Sync"

Write-Host "`n== Checking Python ==" -ForegroundColor Cyan
$exe = $null
foreach ($candidate in @("py -3", "python")) {
    $parts = $candidate -split " "
    $pyArgs = @($parts | Select-Object -Skip 1)
    try {
        & $parts[0] @pyArgs --version 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $exe = $parts[0]; break }
    } catch {}
}
if (-not $exe) {
    Write-Host "Python isn't installed. Install it from https://www.python.org/downloads/windows/" -ForegroundColor Red
    Write-Host "(tick 'Add python.exe to PATH' in the installer), then run Install.bat again."
    exit 1
}

Write-Host "`n== Installing (this takes a few minutes the first time) ==" -ForegroundColor Cyan
if (-not (Test-Path $venv)) { & $exe @pyArgs -m venv $venv }
$venvPython = Join-Path $venv "Scripts\python.exe"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r (Join-Path $here "requirements.txt")
& $venvPython -m playwright install chromium

Write-Host "`n== Your Thermpix login ==" -ForegroundColor Cyan
Write-Host "Saved in Windows Credential Manager on this computer only."
& $venvPython (Join-Path $here "thermpix_sync.py") --set-login

Write-Host "`n== Scheduling the daily 7pm run ==" -ForegroundColor Cyan
$action = New-ScheduledTaskAction -Execute (Join-Path $venv "Scripts\pythonw.exe") `
    -Argument "`"$(Join-Path $here 'thermpix_sync.py')`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Daily -At "7:00PM"
# StartWhenAvailable: if the computer was off at 7pm, run as soon as it's back on.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Downloads new Thermpix patient images to Desktop\DPAs" -Force | Out-Null

Write-Host "`nAll set. It will run every day at 7pm." -ForegroundColor Green
Write-Host "Double-click 'Run Now.bat' to test it straight away."
