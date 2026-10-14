@echo off
rem Connects this sync to Practice Software, so new images are sent there as well as to the DPAs folder.
"%~dp0.venv\Scripts\python.exe" "%~dp0thermpix_sync.py" --connect-practice
pause
