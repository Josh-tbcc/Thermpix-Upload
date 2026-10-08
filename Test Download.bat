@echo off
rem Downloads ONE image from Thermpix into Desktop\DPAs as a test, with the browser visible.
"%~dp0.venv\Scripts\python.exe" "%~dp0thermpix_sync.py" --try-one
pause
