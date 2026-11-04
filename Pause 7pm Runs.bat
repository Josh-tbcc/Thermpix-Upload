@echo off
rem Pauses the 7pm Thermpix run on this computer. Nothing is deleted; Resume 7pm Runs.bat turns it back on.
schtasks /Change /TN "Thermpix DPA Sync" /DISABLE
if errorlevel 1 (echo Could not find the 7pm task - run "Fix 7pm Schedule.bat" first.) else (echo Paused. The 7pm run will not happen until you double-click "Resume 7pm Runs.bat".)
pause
