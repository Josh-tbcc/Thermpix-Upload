@echo off
rem Downloads the latest version of the program from GitHub into this folder.
rem Your saved login, schedule and download history are not touched.
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = 'Tls12'; $base = 'https://raw.githubusercontent.com/Josh-tbcc/Thermpix-Upload/refs/heads/claude/termpix-daily-image-sync-be62qa/'; $ok = $true; foreach ($f in @('thermpix_sync.py', 'Inspect.bat', 'Run Now.bat', 'Test Download.bat', 'Set Up Email.bat', 'Catch Up 10 Days.bat', 'Install.bat', 'install.ps1', 'Uninstall.bat', 'requirements.txt', 'config.example.json', 'README.md')) { try { Invoke-WebRequest -UseBasicParsing -Uri ($base + [uri]::EscapeDataString($f) + '?t=' + (Get-Date).Ticks) -OutFile (Join-Path '%~dp0' $f); Write-Host ('Updated ' + $f) } catch { $ok = $false; Write-Host ('Could not update ' + $f + ': ' + $_.Exception.Message) -ForegroundColor Red } }; try { Invoke-WebRequest -UseBasicParsing -Uri ($base + 'Update.bat?t=' + (Get-Date).Ticks) -OutFile (Join-Path '%~dp0' 'Update.bat.new') } catch {}; if ($ok) { Write-Host 'Up to date.' -ForegroundColor Green }"
pause
rem Replace this file with its new version last (the whole line is read before it runs).
if exist "%~dp0Update.bat.new" move /y "%~dp0Update.bat.new" "%~dp0Update.bat" >nul & exit /b
