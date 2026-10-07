"""Download new patient images from Thermpix (usatherm.com) into Desktop\\DPAs.

Logs in with the username/password saved in Windows Credential Manager,
opens the dashboard, finds the "Recently Created Patients" list and downloads
every patient that hasn't been downloaded before. Patients already downloaded
are remembered in state.json, so each one is only fetched once.

Usage:
    python thermpix_sync.py               run a sync (what the 7pm task does)
    python thermpix_sync.py --set-login   save or change your Thermpix login
    python thermpix_sync.py --headed      run with the browser visible
    python thermpix_sync.py --dry-run     list what would be downloaded
    python thermpix_sync.py --mark-existing
                                          remember everything currently listed
                                          as already downloaded, without
                                          downloading it
"""

import argparse
import getpass
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import keyring
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

APP_NAME = "ThermpixSync"
KEYRING_SERVICE = "ThermpixSync"
HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "base_url": "https://usatherm.com/",
    # Folder name created on the Desktop. Set "output_dir" to a full path to override.
    "folder_name": "DPAs",
    "output_dir": None,
    # Heading text of the dashboard list to download from.
    "section_text": "Recently Created Patients",
    "headless": True,
    "timeout_seconds": 60,
    # Optional CSS selectors. Leave null to let the script find things itself;
    # fill them in only if the automatic detection picks the wrong element.
    "selectors": {
        "login_link": None,
        "username": None,
        "password": None,
        "submit": None,
        "section": None,
        "download": None,
        "patient_link": None,
    },
}

USERNAME_SELECTORS = [
    "input[type=email]",
    "input[autocomplete=username]",
    "input[name*=user i]",
    "input[name*=email i]",
    "input[name*=login i]",
    "input[id*=user i]",
    "input[id*=email i]",
    "input[type=text]",
]
SUBMIT_SELECTORS = [
    "button[type=submit]",
    "input[type=submit]",
    "button:has-text('Log in')",
    "button:has-text('Login')",
    "button:has-text('Sign in')",
]
LOGIN_LINK_SELECTORS = [
    "a:has-text('Log in')",
    "a:has-text('Login')",
    "a:has-text('Sign in')",
    "button:has-text('Log in')",
    "button:has-text('Login')",
    "button:has-text('Sign in')",
]
DOWNLOAD_SELECTOR = ", ".join([
    "a[download]",
    "a:has-text('Download')",
    "button:has-text('Download')",
    "[title*=download i]",
    "[aria-label*=download i]",
    "a[href*=download i]",
])

log = logging.getLogger(APP_NAME)


# --- paths, config and state -------------------------------------------------

def data_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def desktop_dir():
    """The real Desktop folder, including when OneDrive has moved it."""
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
                return Path(os.path.expandvars(value))
        except OSError:
            pass
    return Path.home() / "Desktop"


def load_config():
    config = json.loads(json.dumps(DEFAULTS))
    path = HERE / "config.json"
    if path.exists():
        user = json.loads(path.read_text(encoding="utf-8"))
        config["selectors"].update(user.pop("selectors", {}) or {})
        config.update(user)
    return config


def output_dir(config):
    path = Path(config["output_dir"]) if config["output_dir"] else desktop_dir() / config["folder_name"]
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_state():
    path = data_dir() / "state.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"downloaded": {}}


def save_state(state):
    path = data_dir() / "state.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(data_dir() / "sync.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if sys.stderr:  # pythonw.exe (used by the scheduled task) has no console
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        log.addHandler(console)


# --- credentials -------------------------------------------------------------

def set_login():
    username = input("Thermpix username / email: ").strip()
    password = getpass.getpass("Thermpix password (hidden as you type): ")
    keyring.set_password(KEYRING_SERVICE, "username", username)
    keyring.set_password(KEYRING_SERVICE, username, password)
    print("Login saved to Windows Credential Manager.")


def get_login():
    username = os.environ.get("THERMPIX_USERNAME") or keyring.get_password(KEYRING_SERVICE, "username")
    password = os.environ.get("THERMPIX_PASSWORD") or (
        username and keyring.get_password(KEYRING_SERVICE, username)
    )
    if not username or not password:
        raise SystemExit("No Thermpix login saved. Run: python thermpix_sync.py --set-login")
    return username, password


# --- page helpers ------------------------------------------------------------

def first_visible(scope, selectors):
    for selector in selectors:
        if not selector:
            continue
        locator = scope.locator(selector)
        for i in range(locator.count()):
            if locator.nth(i).is_visible():
                return locator.nth(i)
    return None


def login(page, config, username, password):
    sel = config["selectors"]
    page.goto(config["base_url"], wait_until="domcontentloaded")

    password_sel = sel["password"] or "input[type=password]"
    if not first_visible(page, [password_sel]):
        link = first_visible(page, [sel["login_link"]] + LOGIN_LINK_SELECTORS)
        if link:
            log.info("Opening login page")
            link.click()
        page.locator(password_sel).first.wait_for(state="visible")

    form = page.locator(f"form:has({password_sel})").first
    scope = form if form.count() else page
    user_field = first_visible(scope, [sel["username"]] + USERNAME_SELECTORS)
    if not user_field:
        raise RuntimeError("Couldn't find the username box on the login page")
    user_field.fill(username)
    scope.locator(password_sel).first.fill(password)

    submit = first_visible(scope, [sel["submit"]] + SUBMIT_SELECTORS)
    if submit:
        submit.click()
    else:
        scope.locator(password_sel).first.press("Enter")

    try:
        page.locator(password_sel).first.wait_for(state="hidden")
    except PlaywrightTimeout:
        raise RuntimeError("Login didn't go through - check the saved username/password")
    page.wait_for_load_state("networkidle")
    log.info("Logged in")


def find_section(page, config):
    """The part of the dashboard under the "Recently Created Patients" heading."""
    sel = config["selectors"]
    if sel["section"]:
        section = page.locator(sel["section"]).first
        section.wait_for()
        return section

    pattern = re.compile(re.escape(config["section_text"]), re.I)
    heading = page.get_by_text(pattern).first
    heading.wait_for()
    # Walk up from the heading to the smallest container that holds the list.
    for level in range(1, 10):
        container = heading.locator(f"xpath=ancestor::*[{level}]")
        if container.count() == 0:
            break
        if container.locator("a[href], button").count() > 0:
            return container
    raise RuntimeError(f"Found '{config['section_text']}' but no patients under it")


def row_text(element):
    row = element.locator("xpath=ancestor-or-self::*[self::tr or self::li][1]")
    target = row if row.count() else element
    return " ".join(target.inner_text().split())


def collect_items(page, section, config):
    """Return [(key, label, element)] for each patient in the section.

    If the list has download buttons, each one is an item. Otherwise each
    patient link is an item and the patient's page is searched for downloads.
    """
    sel = config["selectors"]
    downloads = section.locator(sel["download"] or DOWNLOAD_SELECTOR)
    items = []
    if downloads.count():
        for i in range(downloads.count()):
            el = downloads.nth(i)
            label = row_text(el)
            href = el.get_attribute("href")
            key = urljoin(page.url, href) if href and not href.startswith(("#", "javascript")) else label
            items.append((key, label, el, "download"))
        return items

    links = section.locator(sel["patient_link"] or "a[href]")
    seen = set()
    for i in range(links.count()):
        el = links.nth(i)
        href = el.get_attribute("href") or ""
        if href.startswith(("#", "javascript", "mailto")):
            continue
        key = urljoin(page.url, href)
        if key in seen:
            continue
        seen.add(key)
        items.append((key, row_text(el), el, "patient"))
    return items


def unique_path(folder, name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "download"
    path = folder / name
    stem, suffix, n = path.stem, path.suffix, 1
    while path.exists():
        path = folder / f"{stem} ({n}){suffix}"
        n += 1
    return path


def filename_from_response(response, url):
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.I) or re.search(
        r'filename="?([^";]+)"?', disposition, re.I
    )
    if match:
        return unquote(match.group(1).strip())
    return unquote(Path(urlparse(url).path).name) or "download"


def download_element(page, el, folder, timeout_ms):
    """Download what one link/button points at. Returns the saved path."""
    href = el.get_attribute("href")
    if el.evaluate("e => e.tagName") == "A" and href and not href.startswith(("#", "javascript")):
        url = urljoin(page.url, href)
        response = page.request.get(url, timeout=timeout_ms)
        if not response.ok:
            raise RuntimeError(f"Download failed ({response.status}) for {url}")
        if "text/html" not in response.headers.get("content-type", ""):
            path = unique_path(folder, filename_from_response(response, url))
            path.write_bytes(response.body())
            return path
    # A button, or a link that goes through a page: click it and catch the download.
    with page.expect_download(timeout=timeout_ms) as info:
        el.click()
    download = info.value
    path = unique_path(folder, download.suggested_filename)
    download.save_as(path)
    return path


def download_patient(page, el, folder, config, timeout_ms):
    """Open a patient's page and download every file offered there."""
    url = urljoin(page.url, el.get_attribute("href"))
    patient = page.context.new_page()
    try:
        patient.goto(url, wait_until="networkidle")
        buttons = patient.locator(config["selectors"]["download"] or DOWNLOAD_SELECTOR)
        if buttons.count() == 0:
            raise RuntimeError(f"No download buttons found on patient page {url}")
        return [download_element(patient, buttons.nth(i), folder, timeout_ms) for i in range(buttons.count())]
    finally:
        patient.close()


# --- main --------------------------------------------------------------------

def sync(config, headed=False, dry_run=False, mark_existing=False):
    folder = output_dir(config)
    state = load_state()
    username, password = get_login()
    timeout_ms = int(config["timeout_seconds"] * 1000)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config["headless"] and not headed)
        context = browser.new_context(accept_downloads=True)
        context.set_default_timeout(timeout_ms)
        page = context.new_page()
        try:
            login(page, config, username, password)
            section = find_section(page, config)
            items = collect_items(page, section, config)
            new = [item for item in items if item[0] not in state["downloaded"]]
            log.info("%d patients listed, %d new", len(items), len(new))

            saved = 0
            for key, label, el, kind in new:
                if dry_run:
                    log.info("Would download: %s", label)
                    continue
                if mark_existing:
                    files = []
                elif kind == "download":
                    files = [download_element(page, el, folder, timeout_ms)]
                else:
                    files = download_patient(page, el, folder, config, timeout_ms)
                for f in files:
                    log.info("Saved %s", f)
                saved += len(files)
                state["downloaded"][key] = {
                    "label": label,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "files": [f.name for f in files],
                }
                save_state(state)

            if mark_existing:
                log.info("Marked %d patients as already downloaded", len(new))
            elif not dry_run:
                log.info("Done: %d new files in %s", saved, folder)
        except Exception:
            shot = data_dir() / f"error-{datetime.now():%Y%m%d-%H%M%S}.png"
            try:
                page.screenshot(path=str(shot), full_page=True)
                log.error("Screenshot of the page at the time of the error: %s", shot)
            except Exception:
                pass
            raise
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set-login", action="store_true", help="save or change your Thermpix login")
    parser.add_argument("--headed", action="store_true", help="show the browser while it runs")
    parser.add_argument("--dry-run", action="store_true", help="list new patients without downloading")
    parser.add_argument("--mark-existing", action="store_true",
                        help="treat everything currently listed as already downloaded")
    args = parser.parse_args()

    if args.set_login:
        set_login()
        return

    setup_logging()
    try:
        sync(load_config(), headed=args.headed, dry_run=args.dry_run, mark_existing=args.mark_existing)
    except SystemExit:
        raise
    except Exception:
        log.exception("Sync failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
