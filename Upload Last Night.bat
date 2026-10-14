@echo off
rem For when the computer was off at 7pm: downloads the Thermpix images taken
rem yesterday that have not been saved yet. Takes a while - leave this window open.
"%~dp0.venv\Scripts\python.exe" "%~dp0thermpix_sync.py" --yesterday
pause
