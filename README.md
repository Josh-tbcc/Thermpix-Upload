# Thermpix DPA Sync

Every day at 7pm this logs into Thermpix (https://app.thermpix.com/), goes through
every patient under **Patients**, and downloads any image that hasn't been
downloaded before into the clinic's shared folder `\\SERVER\Spinalogic\ImageCapture\DPAs`. That covers new
patients and new images for returning patients. Each file is named after the
patient and the date the image was taken, e.g. `Jane Citizen - 2026-10-07 - scan1.jpg`.

- It uses its own hidden browser, so your Chrome and your download settings are untouched.
- Your login is stored in Windows Credential Manager on your computer, not in this repo.
- Each image is only downloaded once. The list of images already done is kept in
  `%LOCALAPPDATA%\ThermpixSync\state.json`.
- If the computer is off or asleep at 7pm, it runs as soon as it's back on.
- Checking every patient takes a while if you have a lot of them, which is fine at 7pm.
- After every run it emails a report to yandina@thebalancedchiro.com.au: whether it worked,
  how many patients were checked and the name of every image saved (or what went wrong,
  with a screenshot). Set it up once with `Set Up Email.bat`.

## Setup (once, about 5 minutes)

1. **Install Python** from https://www.python.org/downloads/windows/ if you don't have it.
   On the first installer screen, tick **"Add python.exe to PATH"**.
2. **Download this repo**: on GitHub click **Code → Download ZIP**, then unzip it
   somewhere permanent, e.g. `Documents\Thermpix-Upload`. Don't run it from the
   Downloads folder or the zip itself, because the 7pm task runs from wherever you put it.
3. **Double-click `Install.bat`.** It installs what's needed, asks for your Thermpix
   username and password once, downloads the images from the last 10 days (older ones
   are noted as done, not downloaded), and sets up the 7pm schedule.
4. **Double-click `Run Now.bat`** to test it. A browser window opens so you can watch
   it log in and check each patient. New images land in the DPAs folder.

## Day to day

| To... | Do this |
| --- | --- |
| Run it now | Double-click `Run Now.bat` |
| Download one image as a test | Double-click `Test Download.bat` |
| See what would download without downloading | In a command prompt in the folder: `"Run Now.bat" --dry-run` |
| Change your Thermpix password | `.venv\Scripts\python.exe thermpix_sync.py --set-login` |
| Get the latest version of the program | Double-click `Update.bat` |
| Set up or change the emailed run report | Double-click `Set Up Email.bat` |
| Stop the 7pm runs | Double-click `Uninstall.bat` |
| Check what happened | Open `%LOCALAPPDATA%\ThermpixSync\sync.log` |

If a run fails, the log says why, and a screenshot of the page at that moment is
saved next to it (`error-<date>.png`).

## If it can't find things on the page

The script finds the login boxes, the patient list (including
its "Next" pages) and the download buttons by itself. If Thermpix's layout trips it up, copy
`config.example.json` to `config.json` and fill in the CSS selector for the part
it gets wrong, e.g. `"download": "a.btn-download"`. Any field left as `null` keeps
the automatic detection.

## Development

```
pip install -r requirements-dev.txt
playwright install chromium
pytest
```

The tests run the script against a small fake Thermpix site with a paginated
patient list, including a returning patient who gets new images.
