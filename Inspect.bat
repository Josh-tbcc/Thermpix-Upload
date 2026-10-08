@echo off
rem Describes the Thermpix patient list (with patient details blanked out) for troubleshooting.
"%~dp0.venv\Scripts\python.exe" "%~dp0thermpix_sync.py" --inspect
pause
