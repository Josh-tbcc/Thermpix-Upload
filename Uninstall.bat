@echo off
rem Removes the 7pm schedule. Your downloaded images are not touched.
schtasks /Delete /TN "Thermpix DPA Sync" /F
pause
