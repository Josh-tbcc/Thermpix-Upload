@echo off
rem Downloads every Thermpix image from the last 10 days that has not been saved yet.
rem Safe to run any time: images already saved are skipped.
"%~dp0.venv\Scripts\python.exe" "%~dp0thermpix_sync.py" --catch-up-days 10
pause
