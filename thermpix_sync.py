"""Download new patient images from Thermpix (app.thermpix.com) into Desktop\\DPAs.

Logs in with the username/password saved in Windows Credential Manager,
opens the full patient list and checks every patient's page, downloading
each image that hasn't been downloaded before - so new images for returning
patients are picked up too. Files are named "<patient> - <date> - <file>".
Images already downloaded are remembered in state.json, so each is only
fetched once.

Usage:
    python thermpix_sync.py               run a sync (what the 7pm task does)
    python thermpix_sync.py --set-login   save or change your Thermpix login
    python thermpix_sync.py --headed      run with the browser visible
    python thermpix_sync.py --dry-run     list what would be downloaded
    python thermpix_sync.py --mark-existing
                                          remember every image currently listed
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
import time
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
    "base_url": "https://app.thermpix.com/",
    # Folder name created on the Desktop. Set "output_dir" to a full path to override.
    "folder_name": "DPAs",
    "output_dir": None,
    # Menu link that opens the full patient list, and an optional direct URL
    # for that list if the menu link can't be found.
    "patients_link_text": "Patients",
    "patients_url": "https://app.thermpix.com/patients/patients",
    # Safety limit on how many pages of the patient list to walk through.
    "max_list_pages": 200,
    "headless": True,
    "timeout_seconds": 60,
    # Optional CSS selectors. Leave null to let the script find things itself;
    # fill them in only if the automatic detection picks the wrong element.
    "selectors": {
        "login_link": None,
        "username": None,
        "password": None,
        "submit": None,
        "patients_link": None,
        "patient_link": None,
        "next_page": None,
        "download": None,
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
ROW_SELECTOR = "tbody tr, mat-row, [role=row]"
NEXT_PAGE_SELECTORS = [
    "a[rel=next]",
    "[aria-label*=next i]",
    "a:text-is('Next')",
    "button:text-is('Next')",
    "a:has-text('Next ')",
    "a:text-is('›')",
    "a:text-is('»')",
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


def open_patient_list(page, config):
    """Go from the dashboard to the page listing all patients."""
    sel = config["selectors"]
    if config["patients_url"]:
        page.goto(urljoin(page.url, config["patients_url"]), wait_until="networkidle")
        return
    pattern = re.compile(rf"^\s*(all\s+)?{re.escape(config['patients_link_text'])}\b", re.I)
    # The menu item might be a link, a button, a menu entry or just text with a
    # click handler, so try each kind until one shows up.
    candidates = [page.locator(sel["patients_link"])] if sel["patients_link"] else [
        page.get_by_role("link", name=pattern),
        page.get_by_role("menuitem", name=pattern),
        page.get_by_role("button", name=pattern),
        page.get_by_role("tab", name=pattern),
        page.get_by_text(pattern),
    ]
    link = None
    deadline = time.monotonic() + config["timeout_seconds"]
    while link is None and time.monotonic() < deadline:
        for locator in candidates:
            for i in range(locator.count()):
                if locator.nth(i).is_visible():
                    link = locator.nth(i)
                    break
            if link:
                break
        else:
            page.wait_for_timeout(500)
    if link is None:
        raise RuntimeError(
            f"Couldn't find the '{config['patients_link_text']}' menu link. "
            "Set patients_url in config.json to the address of the patient list."
        )
    link.click()
    page.wait_for_load_state("networkidle")
    log.info("Opened patient list: %s", page.url)


def patient_links_on_page(page, config):
    """{url: name} for the patient links in the list on the current page."""
    sel = config["selectors"]
    if sel["patient_link"]:
        candidates = page.locator(sel["patient_link"])
    else:
        # The first link in each table row / list item of the main content.
        candidates = page.locator(
            "xpath=//*[self::tr or self::li or @role='row'][not(ancestor::nav or ancestor::header "
            "or ancestor::footer or ancestor::aside)]/descendant::a[@href][1]"
        )
    patients = {}
    for i in range(candidates.count()):
        el = candidates.nth(i)
        href = el.get_attribute("href") or ""
        name = " ".join(el.inner_text().split())
        if not name or href.startswith(("#", "javascript", "mailto")) or "download" in name.lower():
            continue
        url = urljoin(page.url, href)
        if url != page.url:
            patients.setdefault(url, name)
    return patients


def data_rows(page):
    """The patient rows in the list table (header rows left out)."""
    rows = page.locator(ROW_SELECTOR)
    return [rows.nth(i) for i in range(rows.count())
            if rows.nth(i).is_visible() and rows.nth(i).locator("th, [role=columnheader]").count() == 0]


def first_row_text(page):
    rows = data_rows(page)
    return rows[0].inner_text() if rows else ""


def wait_for_rows(page, config):
    deadline = time.monotonic() + config["timeout_seconds"]
    while not data_rows(page) and time.monotonic() < deadline:
        page.wait_for_timeout(500)


def click_next(page, config):
    """Go to the next page of the patient list. False when there isn't one."""
    next_link = first_visible(page, [config["selectors"]["next_page"]] + NEXT_PAGE_SELECTORS)
    if not next_link or next_link.get_attribute("disabled") is not None or \
            next_link.get_attribute("aria-disabled") == "true" or \
            "disabled" in (next_link.get_attribute("class") or ""):
        return False
    before = first_row_text(page)
    next_link.click()
    page.wait_for_load_state("networkidle")
    # In a web app the rows change without a page load, so wait for them to.
    deadline = time.monotonic() + 15
    while first_row_text(page) == before and time.monotonic() < deadline:
        page.wait_for_timeout(300)
    return first_row_text(page) != before


def patients_by_clicking_rows(page, config, page_number):
    """{url: name} for list rows that open the patient when clicked (no links)."""
    patients = {}
    page_start = first_row_text(page)
    count = len(data_rows(page))
    for i in range(count):
        rows = data_rows(page)
        if i >= len(rows):
            break
        row = rows[i]
        first_cell = row.locator("td, [role=cell], [role=gridcell], mat-cell").first
        name = " ".join((first_cell if first_cell.count() else row).inner_text().split())
        before = page.url
        (first_cell if first_cell.count() else row).click()
        try:
            page.wait_for_url(lambda url: url != before, timeout=10000)
        except PlaywrightTimeout:
            continue  # this row doesn't open anything
        page.wait_for_load_state("networkidle")
        patients.setdefault(page.url, name or "Unknown patient")
        page.go_back()
        page.wait_for_load_state("networkidle")
        wait_for_rows(page, config)
        # Some lists jump back to page 1 after going back; return to our page.
        if first_row_text(page) != page_start:
            for _ in range(page_number - 1):
                click_next(page, config)
    return patients


def all_patients(page, config):
    """Walk every page of the patient list and return {url: name}."""
    open_patient_list(page, config)
    wait_for_rows(page, config)
    patients = {}
    for page_number in range(1, config["max_list_pages"] + 1):
        found = patient_links_on_page(page, config) or patients_by_clicking_rows(page, config, page_number)
        new = {url: name for url, name in found.items() if url not in patients}
        patients.update(new)
        log.info("Patient list page %d: %d patients", page_number, len(found))
        if not new or not click_next(page, config):
            break
    if not patients:
        raise RuntimeError(
            f"No patients found on the patient list page ({page.url}): "
            f"{len(data_rows(page))} table rows, {page.locator('a[href]').count()} links"
        )
    return patients


def row_label(el):
    row = el.locator("xpath=ancestor-or-self::*[self::tr or self::li][1]")
    return " ".join((row if row.count() else el).inner_text().split())


def image_key(page, el, fallback):
    """A stable id for one downloadable image, so it's only fetched once."""
    href = el.get_attribute("href")
    if href and not href.startswith(("#", "javascript")):
        return urljoin(page.url, href)
    return fallback


def iter_images(page, patients, config):
    """Yield (key, patient name, page, element) for every download on each patient's page."""
    download_sel = config["selectors"]["download"] or DOWNLOAD_SELECTOR
    for url, name in patients.items():
        patient_page = page.context.new_page()
        try:
            patient_page.goto(url, wait_until="networkidle")
            buttons = patient_page.locator(download_sel)
            if buttons.count() == 0:
                log.warning("No download buttons found for %s (%s)", name, url)
            seen = {}
            for i in range(buttons.count()):
                el = buttons.nth(i)
                # Buttons without a link are told apart by their label (and the
                # row they're in), not their position, which shifts as images are added.
                label = row_label(el)
                seen[label] = seen.get(label, 0) + 1
                yield image_key(patient_page, el, f"{url}#{label}#{seen[label]}"), name, patient_page, el
        finally:
            patient_page.close()


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


def download_element(page, el, folder, prefix, timeout_ms):
    """Download what one link/button points at, named "<prefix> - <original name>"."""
    href = el.get_attribute("href")
    if el.evaluate("e => e.tagName") == "A" and href and not href.startswith(("#", "javascript")):
        url = urljoin(page.url, href)
        response = page.request.get(url, timeout=timeout_ms)
        if not response.ok:
            raise RuntimeError(f"Download failed ({response.status}) for {url}")
        if "text/html" not in response.headers.get("content-type", ""):
            path = unique_path(folder, f"{prefix} - {filename_from_response(response, url)}")
            path.write_bytes(response.body())
            return path
    # A button, or a link that goes through a page: click it and catch the download.
    with page.expect_download(timeout=timeout_ms) as info:
        el.click()
    download = info.value
    path = unique_path(folder, f"{prefix} - {download.suggested_filename}")
    download.save_as(path)
    return path


# --- main --------------------------------------------------------------------

def sync(config, headed=False, dry_run=False, mark_existing=False):
    folder = output_dir(config)
    state = load_state()
    username, password = get_login()
    timeout_ms = int(config["timeout_seconds"] * 1000)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config["headless"] and not headed)
        context = browser.new_context(accept_downloads=True, viewport={"width": 1600, "height": 1000})
        context.set_default_timeout(timeout_ms)
        page = context.new_page()
        try:
            login(page, config, username, password)
            patients = all_patients(page, config)
            log.info("Checking %d patients for new images", len(patients))
            today = datetime.now().strftime("%Y-%m-%d")

            listed = saved = 0
            for key, name, owner, el in iter_images(page, patients, config):
                listed += 1
                if key in state["downloaded"]:
                    continue
                if dry_run:
                    log.info("Would download an image for %s", name)
                    continue
                file = None
                if not mark_existing:
                    file = download_element(owner, el, folder, f"{name} - {today}", timeout_ms)
                    log.info("Saved %s", file)
                    saved += 1
                state["downloaded"][key] = {
                    "patient": name,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "file": file.name if file else None,
                }
                save_state(state)

            log.info("%d images listed", listed)
            if mark_existing:
                log.info("Marked all current images as already downloaded")
            elif not dry_run:
                log.info("Done: %d new images saved in %s", saved, folder)
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
