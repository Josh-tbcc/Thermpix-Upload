@echo off
rem Runs the sync now with the browser visible, so you can watch it work.
"%~dp0.venv\Scripts\python.exe" "%~dp0thermpix_sync.py" --headed %*
pause
