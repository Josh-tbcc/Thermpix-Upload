@echo off
rem Sets up the email report sent after every run (and sends a test email).
"%~dp0.venv\Scripts\python.exe" "%~dp0thermpix_sync.py" --set-email
pause
