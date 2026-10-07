# Thermpix DPA Sync

Every day at 7pm this logs into Thermpix (https://usatherm.com/), looks at
**Recently Created Patients** on the dashboard, and downloads any image that
hasn't been downloaded before into a folder called **DPAs** on your Desktop.
New images for a returning patient are picked up too. Each file is named after
the patient, e.g. `Jane Citizen - 2026-10-07 - scan1.jpg`.

- It uses its own hidden browser, so your Chrome and your download settings are untouched.
- Your login is stored in Windows Credential Manager on your computer, not in this repo.
- Each image is only downloaded once. The list of images already done is kept in
  `%LOCALAPPDATA%\ThermpixSync\state.json`.
- If the computer is off or asleep at 7pm, it runs as soon as it's back on.

## Setup (once, about 5 minutes)

1. **Install Python** from https://www.python.org/downloads/windows/ if you don't have it.
   On the first installer screen, tick **"Add python.exe to PATH"**.
2. **Download this repo**: on GitHub click **Code → Download ZIP**, then unzip it
   somewhere permanent, e.g. `Documents\Thermpix-Upload`. Don't run it from the
   Downloads folder or the zip itself, because the 7pm task runs from wherever you put it.
3. **Double-click `Install.bat`.** It installs what's needed, asks for your Thermpix
   username and password once, and sets up the 7pm schedule.
4. **Double-click `Run Now.bat`** to test it. A browser window opens so you can watch
   it log in and download. Check the new files in `Desktop\DPAs`.

### Don't want the old patients?

The first run downloads every image currently available for the patients in the
Recently Created Patients list. To skip those and only get images taken from now on, run this once from the folder
instead of `Run Now.bat`:

```
.venv\Scripts\python.exe thermpix_sync.py --mark-existing
```

## Day to day

| To... | Do this |
| --- | --- |
| Run it now | Double-click `Run Now.bat` |
| See what would download without downloading | In a command prompt in the folder: `"Run Now.bat" --dry-run` |
| Change your Thermpix password | `.venv\Scripts\python.exe thermpix_sync.py --set-login` |
| Stop the 7pm runs | Double-click `Uninstall.bat` |
| Check what happened | Open `%LOCALAPPDATA%\ThermpixSync\sync.log` |

If a run fails, the log says why, and a screenshot of the page at that moment is
saved next to it (`error-<date>.png`).

## If it can't find things on the page

The script finds the login boxes, the Recently Created Patients list and the
download buttons by itself. If Thermpix's layout trips it up, copy
`config.example.json` to `config.json` and fill in the CSS selector for the part
it gets wrong, e.g. `"download": "a.btn-download"`. Any field left as `null` keeps
the automatic detection.

## Development

```
pip install -r requirements-dev.txt
playwright install chromium
pytest
```

The tests run the script against a small fake Thermpix site, covering both layouts
it supports: download buttons directly in the patient list, or patient links that
open a page with the download button.
