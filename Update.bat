@echo off
rem Downloads the latest version of the program from GitHub into this folder.
rem Your saved login, schedule and download history are not touched.
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = 'Tls12'; $base = 'https://raw.githubusercontent.com/Josh-tbcc/Thermpix-Upload/refs/heads/claude/termpix-daily-image-sync-be62qa/'; $ok = $true; foreach ($f in @('thermpix_sync.py', 'Inspect.bat', 'Run Now.bat', 'Install.bat', 'install.ps1', 'Uninstall.bat', 'requirements.txt')) { try { Invoke-WebRequest -UseBasicParsing -Uri ($base + [uri]::EscapeDataString($f) + '?t=' + (Get-Date).Ticks) -OutFile (Join-Path '%~dp0' $f); Write-Host ('Updated ' + $f) } catch { $ok = $false; Write-Host ('Could not update ' + $f + ': ' + $_.Exception.Message) -ForegroundColor Red } }; if ($ok) { Write-Host 'Up to date.' -ForegroundColor Green }"
pause
