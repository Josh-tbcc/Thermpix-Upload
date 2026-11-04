@echo off
rem Turns the paused 7pm Thermpix run back on.
schtasks /Change /TN "Thermpix DPA Sync" /ENABLE
if errorlevel 1 (echo Could not find the 7pm task - run "Fix 7pm Schedule.bat" to set it up again.) else (echo Resumed. It will run again every day at 7pm.)
pause
