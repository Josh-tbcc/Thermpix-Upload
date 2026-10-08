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
import zipfile
from datetime import date, datetime, timedelta
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
    # Where the images are saved: the clinic's shared DPAs folder. Set to null
    # to use a folder called "folder_name" on the Desktop instead.
    "folder_name": "DPAs",
    "output_dir": r"\\SERVER\Spinalogic\ImageCapture\DPAs",
    # Menu link that opens the full patient list, and an optional direct URL
    # for that list if the menu link can't be found.
    "patients_link_text": "Patients",
    # Section of the patient file that holds the images, and the buttons used
    # there: "Select image" turns on the checkboxes, "Download" fetches the ticked ones.
    "images_tab_text": "Images",
    "select_text": "select( images?)?",
    "download_text": "download( images?)?",
    "patients_url": "/patients/patients",
    # Safety limit on how many pages of the patient list to walk through.
    "max_list_pages": 200,
    "headless": True,
    "timeout_seconds": 60,
    # How long to wait for buttons and sections to appear in the app.
    "ui_wait_seconds": 10,
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
        "open_patient": None,
        "images_tab": None,
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
# The book icon in each patient row that opens the patient's file. In
# Thermpix it's a button labelled "Details" (next to "Edit", never clicked).
BOOK_ICON_SELECTOR = ", ".join([
    "button[aria-label='Details' i]",
    "[mattooltip='Details' i]",
    "mat-icon:text-matches('^\\s*(book|menu_book|import_contacts|auto_stories|library_books|book_2)\\s*$', 'i')",
    "[fonticon*=book i]",
    "[svgicon*=book i]",
    "[data-icon*=book i]",
    "[class*=book i]",
])
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
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"Can't open the folder {path} - is the server on and reachable? ({e})")
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
    # Let the web app finish loading (and redirecting to its login page) before
    # typing, or the form can be redrawn and lose what was typed.
    page.goto(config["base_url"], wait_until="networkidle")

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

    # Logged in once we've left the login page and no password box is showing.
    deadline = time.monotonic() + config["timeout_seconds"]
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if "login" not in urlparse(page.url).path.lower() and not first_visible(page, [password_sel]):
            break
    else:
        raise RuntimeError(f"Login didn't go through. {login_page_message(page)}")
    page.wait_for_load_state("networkidle")
    log.info("Logged in (now at %s)", urlparse(page.url).path)


def login_page_message(page):
    """Any error or prompt the login page is showing, to explain a failed login."""
    messages = page.locator(
        "[role=alert], .error, .alert, .invalid-feedback, mat-error, .mat-mdc-form-field-error, "
        ".toast, .snackbar, mat-snack-bar-container"
    )
    texts = [" ".join(messages.nth(i).inner_text().split()) for i in range(messages.count())
             if messages.nth(i).is_visible()]
    texts = [t for t in texts if t]
    if texts:
        return "Thermpix says: " + " / ".join(dict.fromkeys(texts))
    if first_visible(page, ["input[autocomplete=one-time-code]", "input[name*=code i]", "input[name*=otp i]"]):
        return "Thermpix is asking for a verification code."
    return "Check the saved username/password (run Install.bat again to re-enter them)."


def on_login_page(page):
    return "login" in urlparse(page.url).path.lower()


def app_navigate(page, url, config):
    """Open a page inside the web app without reloading it.

    Thermpix only remembers the login while you stay inside the app, so typing
    an address (a full page load) logs you out. Changing the address the way
    the app's own links do keeps the login.
    """
    target = urlparse(urljoin(page.url, url))
    path = target.path + (f"?{target.query}" if target.query else "")
    before = page.locator("body").inner_text()
    page.evaluate(
        """path => {
            history.pushState({}, '', path);
            window.dispatchEvent(new PopStateEvent('popstate', {state: {}}));
        }""",
        path,
    )
    page.wait_for_load_state("networkidle")
    deadline = time.monotonic() + config["ui_wait_seconds"] / 2
    while page.locator("body").inner_text() == before and time.monotonic() < deadline:
        page.wait_for_timeout(250)
    if page.locator("body").inner_text() == before:
        # Nothing redrew, so this is an ordinary website: load the page normally.
        page.goto(urljoin(page.url, url), wait_until="networkidle")
    if on_login_page(page):
        raise RuntimeError(f"Thermpix logged us out when opening {path}")


def find_menu_item(page, config, wait_seconds):
    """The visible "Patients" menu item, or None."""
    sel = config["selectors"]
    word = re.escape(config["patients_link_text"])
    # Menu names can start with an icon's name, e.g. "groups Patients".
    name = re.compile(rf"^\s*(\w+\s+)?(all\s+)?{word}\s*$", re.I)
    # The menu item might be a link, a button, a menu entry or just text with a
    # click handler, so try each kind until one shows up.
    candidates = [page.locator(sel["patients_link"])] if sel["patients_link"] else [
        page.get_by_role("link", name=name),
        page.get_by_role("menuitem", name=name),
        page.get_by_role("button", name=name),
        page.get_by_role("tab", name=name),
        page.get_by_text(re.compile(rf"^\s*{word}\s*$", re.I)),
        page.locator(f"a[href$='/{config['patients_link_text'].lower()}']"),
    ]
    deadline = time.monotonic() + wait_seconds
    while True:
        for locator in candidates:
            for i in range(locator.count()):
                if locator.nth(i).is_visible():
                    return locator.nth(i)
        if time.monotonic() > deadline:
            return None
        page.wait_for_timeout(500)


def open_patient_list(page, config):
    """Go from the dashboard to the page listing all patients."""
    start = page.url
    link = find_menu_item(page, config, wait_seconds=config["ui_wait_seconds"])
    if link:
        link.click()
        page.wait_for_load_state("networkidle")
        try:
            page.wait_for_url(lambda url: url != start, timeout=config["ui_wait_seconds"] * 1000)
        except PlaywrightTimeout:
            pass
    if page.url == start:
        # The click didn't leave the dashboard (whose "Recently Created Patients"
        # table is not the full list), so go to the patient list directly.
        if not config["patients_url"]:
            raise RuntimeError(f"Couldn't open the '{config['patients_link_text']}' page")
        log.info("Opening %s directly", config["patients_url"])
        app_navigate(page, config["patients_url"], config)
    if on_login_page(page):
        raise RuntimeError("Thermpix sent us back to the login page when opening the patient list")
    log.info("Opened patient list: %s", urlparse(page.url).path)


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
    """Wait for the list to finish loading: rows present and not changing for 3 seconds.

    Web apps often show a single "loading" row first and fill in the real
    patients a moment later.
    """
    deadline = time.monotonic() + config["timeout_seconds"]
    last, stable_since = None, time.monotonic()
    while time.monotonic() < deadline:
        rows = data_rows(page)
        snapshot = (len(rows), rows[0].inner_text() if rows else "")
        if snapshot != last:
            last, stable_since = snapshot, time.monotonic()
        elif rows and time.monotonic() - stable_since >= 3:
            return
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
    deadline = time.monotonic() + config["ui_wait_seconds"]
    while first_row_text(page) == before and time.monotonic() < deadline:
        page.wait_for_timeout(300)
    return first_row_text(page) != before


def name_columns(page):
    """Positions of the columns that hold the patient's name, from the headings."""
    headers = page.locator("thead th, mat-header-cell, [role=columnheader]")
    texts = [" ".join(headers.nth(i).inner_text().split()).lower() for i in range(headers.count())]
    columns = [i for i, t in enumerate(texts)
               if "name" in t and not any(w in t for w in ("user", "clinic", "device", "entity", "practitioner"))]
    # "First name" before "Last name", whatever order the table shows them in.
    return sorted(columns, key=lambda i: 0 if any(w in texts[i] for w in ("first", "given")) else 1)


def row_patient_name(row, columns):
    cells = row.locator("td, [role=cell], [role=gridcell], mat-cell")
    texts = [" ".join(cells.nth(i).inner_text().split()) for i in range(cells.count())]
    picked = [texts[i] for i in columns if i < len(texts) and texts[i]]
    if not picked:
        picked = [t for t in texts if t][:1]
    return " ".join(picked)


def open_patient_control(row, config):
    """What to click in a list row to open the patient's file: the book icon if there is one."""
    icon = row.locator(config["selectors"]["open_patient"] or BOOK_ICON_SELECTOR).first
    if icon.count():
        clickable = icon.locator("xpath=ancestor-or-self::*[self::button or self::a or @role='button'][1]")
        return clickable if clickable.count() else icon
    first_cell = row.locator("td, [role=cell], [role=gridcell], mat-cell").first
    return first_cell if first_cell.count() else row


def patients_by_clicking_rows(page, config, page_number, limit=None):
    """{url: name} for list rows whose patient file opens on click (no links)."""
    patients = {}
    columns = name_columns(page)
    page_start = first_row_text(page)
    count = min(len(data_rows(page)), limit or 10**9)
    log.info("%d rows in the list; opening each with %s", count,
             "the book icon" if data_rows(page) and data_rows(page)[0].locator(
                 config["selectors"]["open_patient"] or BOOK_ICON_SELECTOR).count() else "a click on the row")
    for i in range(count):
        rows = data_rows(page)
        if i >= len(rows):
            break
        row = rows[i]
        name = row_patient_name(row, columns)
        before = page.url
        open_patient_control(row, config).click()
        try:
            page.wait_for_url(lambda url: url != before, timeout=10000)
        except PlaywrightTimeout:
            log.info("Row %d of %d didn't open a patient file", i + 1, count)
            if i >= 2 and not patients:
                raise RuntimeError("Clicking the patients in the list doesn't open their file")
            continue  # this row doesn't open anything
        page.wait_for_load_state("networkidle")
        patients.setdefault(page.url, name)
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


def open_images_tab(page, config):
    """Click "Images" in the patient's file, if the file has such a section."""
    sel = config["selectors"]
    if sel["images_tab"]:
        candidates = [page.locator(sel["images_tab"])]
    else:
        pattern = re.compile(rf"^\s*{re.escape(config['images_tab_text'])}\b", re.I)
        candidates = [page.get_by_role(role, name=pattern) for role in ("tab", "link", "button", "menuitem")]
        candidates.append(page.get_by_text(re.compile(rf"^\s*{re.escape(config['images_tab_text'])}\s*$", re.I)))
    deadline = time.monotonic() + config["ui_wait_seconds"]
    while time.monotonic() < deadline:
        for locator in candidates:
            for i in range(locator.count()):
                if locator.nth(i).is_visible():
                    locator.nth(i).click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(1000)
                    return True
        page.wait_for_timeout(500)
    log.warning("No '%s' section found in the patient file at %s", config["images_tab_text"], urlparse(page.url).path)
    return False


def images_area(page):
    """The main part of the patient file, without the side panel (photo, name, menu)."""
    area = page.locator(".layout-ov-page > :not(.layout-ov-page__aside)")
    if area.count():
        return area
    for selector in ("main", "body"):
        if page.locator(selector).count():
            return page.locator(selector).first
    return page.locator("body")


def find_visible(page, candidates, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    while True:
        for locator in candidates:
            for i in range(locator.count()):
                el = locator.nth(i)
                if el.is_visible() and el.is_enabled():
                    return el
        if time.monotonic() > deadline:
            return None
        page.wait_for_timeout(500)


def button_named(page, words, wait_seconds):
    """A visible, enabled button/menu entry whose name is `words` (an icon name may come first)."""
    name = re.compile(rf"^\s*(\w+\s+)?{words}\s*$", re.I)
    candidates = [page.get_by_role(role, name=name) for role in ("button", "menuitem", "tab", "link")]
    candidates.append(page.get_by_text(re.compile(rf"^\s*{words}\s*$", re.I)))
    return find_visible(page, candidates, wait_seconds)


def save_download(download, folder, prefix):
    """Save a browser download as "<prefix> - <name>"; unpack zips into separate images."""
    name = download.suggested_filename
    if not name.lower().endswith(".zip"):
        path = unique_path(folder, f"{prefix} - {name}")
        download.save_as(path)
        return [path]
    tmp = data_dir() / "last-download.zip"
    download.save_as(tmp)
    saved = []
    with zipfile.ZipFile(tmp) as z:
        for member in z.infolist():
            if member.is_dir():
                continue
            path = unique_path(folder, f"{prefix} - {Path(member.filename).name}")
            path.write_bytes(z.read(member))
            saved.append(path)
    tmp.unlink()
    return saved


# Day/month order of numeric dates like 03/10/2026, learned from dates where
# one part is over 12 (e.g. 25/09/2026 can only be day-first).
DATE_ORDER = {"order": None}
MONTH_FORMATS = ["%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y"]


def learn_date_order(texts):
    for text in texts:
        for a, b, _ in re.findall(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", text):
            if int(a) > 12:
                DATE_ORDER["order"] = "dmy"
            elif int(b) > 12:
                DATE_ORDER["order"] = "mdy"


def possible_dates(text):
    """Every date the text could mean (two when day/month order is unknown)."""
    text = " ".join(text.split())
    if re.search(r"\btoday\b", text, re.I):
        return [date.today()]
    if re.search(r"\byesterday\b", text, re.I):
        return [date.today() - timedelta(days=1)]
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if m:
        try:
            return [date(int(m[1]), int(m[2]), int(m[3]))]
        except ValueError:
            return []
    m = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", text)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        orders = [DATE_ORDER["order"]] if DATE_ORDER["order"] else ["dmy", "mdy"]
        found = []
        for order in orders:
            d, mo = (a, b) if order == "dmy" else (b, a)
            try:
                found.append(date(y, mo, d))
            except ValueError:
                pass
        return list(dict.fromkeys(found))
    # "Oct 3, 2026", "3 October 2026", "Sat, Oct 3, 2026"...
    for m in re.finditer(r"(\d{1,2} [A-Za-z]{3,9}|[A-Za-z]{3,9} \d{1,2}),? (\d{4})", text):
        candidate = f"{m[1]} {m[2]}"
        for fmt in MONTH_FORMATS:
            try:
                return [datetime.strptime(candidate, fmt).date()]
            except ValueError:
                continue
    return []


def image_dates(card):
    """Possible dates an image was taken, from its date heading (or its own card)."""
    group = card.locator("xpath=ancestor-or-self::*[contains(concat(' ', normalize-space(@class), ' '), ' image-group ')][1]")
    header = group.locator(".image-group__header").first if group.count() else None
    for text in ([header.inner_text()] if header is not None and header.count() else []) + [card.inner_text()]:
        dates = possible_dates(text)
        if dates:
            return dates
    return []


def iter_selectable_images(page, url, name, config, timeout_ms):
    """Images that are downloaded via "Select image", ticking the image, then "Download".

    Yields (key, download) per image; download(prefix) ticks just that image,
    presses Download, saves the file(s) and unticks it again.
    """
    select = button_named(page, config["select_text"], wait_seconds=config["ui_wait_seconds"])
    if not select:
        return
    select.click()
    page.wait_for_timeout(1000)
    area = images_area(page)
    boxes = [area.nth(a).locator("input[type=checkbox]").nth(b)
             for a in range(area.count()) for b in range(area.nth(a).locator("input[type=checkbox]").count())]
    if not boxes:
        log.info("No images for %s", name)
        return
    headers = page.locator(".image-group__header")
    learn_date_order([headers.nth(i).inner_text() for i in range(headers.count())])
    seen = {}
    for box in boxes:
        # The image's card: the largest area around the checkbox holding no other checkbox.
        card = box.locator("xpath=ancestor::*[count(.//input[@type='checkbox'])=1][last()]")
        picture = card.locator("img, canvas").first
        if not picture.count():
            continue  # e.g. a "select all" box
        src = picture.get_attribute("src") or ""
        if src and not src.startswith(("data:", "blob:")):
            key = f"{url}|{urlparse(urljoin(page.url, src)).path}"
        else:
            label = " ".join(card.inner_text().split())
            seen[label] = seen.get(label, 0) + 1
            key = f"{url}|{label}#{seen[label]}"

        def download(prefix, box=box):
            box.check(force=True)
            button = button_named(page, config["download_text"], wait_seconds=config["ui_wait_seconds"])
            if not button:
                raise RuntimeError(f"No '{config['download_text']}' button after ticking an image")
            with page.expect_download(timeout=timeout_ms) as info:
                button.click()
            files = save_download(info.value, output_dir(config), prefix)
            box.uncheck(force=True)
            return files

        yield key, download, image_dates(card)


def iter_download_buttons(page, url, name, config, timeout_ms):
    """Images offered as download links/buttons directly on the page."""
    buttons = page.locator(config["selectors"]["download"] or DOWNLOAD_SELECTOR)
    # The app draws the page after loading its data, so give it a moment.
    deadline = time.monotonic() + config["ui_wait_seconds"]
    while buttons.count() == 0 and time.monotonic() < deadline:
        page.wait_for_timeout(500)
    if buttons.count() == 0:
        log.warning("No images found for %s (%s)", name, urlparse(url).path)
    seen = {}
    for i in range(buttons.count()):
        el = buttons.nth(i)
        # Buttons without a link are told apart by their label (and the
        # row they're in), not their position, which shifts as images are added.
        label = row_label(el)
        seen[label] = seen.get(label, 0) + 1

        def download(prefix, el=el):
            return [download_element(page, el, output_dir(config), prefix, timeout_ms)]

        yield image_key(page, el, f"{url}#{label}#{seen[label]}"), download, possible_dates(label)


def name_in_patient_file(page):
    """The patient's name shown under their photo at the top of their file."""
    header = page.locator(".ov-header__text p, .ov-header__text h1, .ov-header__text h2").first
    try:
        header.wait_for(timeout=5000)
    except PlaywrightTimeout:
        return ""
    return " ".join(header.inner_text().split())


def iter_images(page, patients, config, timeout_ms):
    """Yield (key, patient name, download, possible dates) for every image of every patient."""
    for url, name in patients.items():
        app_navigate(page, url, config)
        if not name or name == "Unknown patient":
            name = name_in_patient_file(page) or "Unknown patient"
        open_images_tab(page, config)
        found = False
        for key, download, dates in iter_selectable_images(page, url, name, config, timeout_ms):
            found = True
            yield key, name, download, dates
        if not found:
            for key, download, dates in iter_download_buttons(page, url, name, config, timeout_ms):
                yield key, name, download, dates


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
        # Not allowed (the app may need its own login token) or a web page:
        # fall through and click it like a person would instead.
        if response.ok and "text/html" not in response.headers.get("content-type", ""):
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


# --- inspect -----------------------------------------------------------------

# Describes an element's structure with all patient text blanked out: tags,
# classes and roles are kept; text becomes [text N] (N = length) except icon
# names and column headings; numbers in addresses become N.
DESCRIBE_JS = """
(el) => {
  const SAFE_WORDS = /^(view|open|edit|delete|remove|download|details?|more|actions?|menu|next|previous|page|images?|scans?|reports?|export|patients?|close|back|select)\\b/i;
  const ICON = (n) => n.matches && n.matches("mat-icon, .material-icons, .material-symbols-outlined, i[class*=icon], i[class*=fa]");
  function attrs(n) {
    let out = "";
    for (const a of n.attributes) {
      if (a.name.startsWith("_ng") || a.name === "style") continue;
      let v = a.value;
      if (["aria-label", "title", "mattooltip", "ng-reflect-message", "alt", "placeholder"].includes(a.name)) {
        v = SAFE_WORDS.test(v.trim()) ? v : "[redacted]";
      } else if (a.name === "value") {
        v = "[redacted]";
      } else {
        v = v.replace(/\\d+/g, "N").slice(0, 80);
      }
      out += ` ${a.name}="${v}"`;
    }
    const cs = getComputedStyle(n);
    if (cs.cursor === "pointer") out += " [CLICKABLE]";
    return out;
  }
  function walk(n, depth, keepText) {
    const pad = "  ".repeat(depth);
    if (n.nodeType === 3) {
      const t = n.textContent.trim();
      if (!t) return "";
      return pad + (keepText ? t.slice(0, 40) : `[text ${t.length}]`) + "\\n";
    }
    if (n.nodeType !== 1) return "";
    const tag = n.tagName.toLowerCase();
    if (["script", "style", "path", "g"].includes(tag)) return "";
    if (tag === "svg") return `${pad}<svg>\\n`;
    const keep = keepText || ICON(n) || n.matches("th, [role=columnheader], mat-header-cell") ||
      (n.matches("button, [role=button]") && SAFE_WORDS.test(n.textContent.trim()));
    let out = `${pad}<${tag}${attrs(n)}>\\n`;
    if (depth > 12) return out + pad + "  ...\\n";
    for (const c of n.childNodes) out += walk(c, depth + 1, keep);
    return out;
  }
  return walk(el, 0, false);
}
"""


def inspect(config, headed=True):
    """Print a privacy-safe description of the patient list, for troubleshooting."""
    username, password = get_login()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        context.set_default_timeout(int(config["timeout_seconds"] * 1000))
        page = context.new_page()
        try:
            login(page, config, username, password)
            open_patient_list(page, config)
            wait_for_rows(page, config)
            page.wait_for_timeout(2000)
            print("\n===== COPY FROM HERE =====")
            print("Address:", re.sub(r"\d+", "N", urlparse(page.url).path))
            for selector in ["table", "tbody tr", "mat-row", "[role=row]", "a[href]", "button",
                             "[role=dialog]", "mat-paginator", "[aria-label*=next i]"]:
                print(f"count {selector}: {page.locator(selector).count()}")
            header = page.locator("thead tr, mat-header-row, [role=row]:has([role=columnheader])").first
            if header.count():
                print("\n--- header row ---")
                print(header.evaluate(DESCRIBE_JS))
            rows = data_rows(page)
            print("patient rows found:", len(rows), "| name columns:", name_columns(page))
            for i, row in enumerate(rows[:2]):
                print(f"--- patient row {i + 1} (names blanked) ---")
                print(row.evaluate(DESCRIBE_JS))
            print("--- links on the page (outside the list) ---")
            links = page.locator("a[href]")
            for i in range(links.count()):
                link = links.nth(i)
                in_rows = link.evaluate("e => !!e.closest('tr, mat-row, [role=row]')")
                text = " ".join(link.inner_text().split())
                print(" ", "[in a row]" if in_rows else text[:30], "->",
                      re.sub(r"\d+", "N", urlparse(urljoin(page.url, link.get_attribute("href"))).path))
            if rows:
                print("--- opening the first patient (book icon if found) ---")
                before = page.url
                open_patient_control(rows[0], config).click()
                page.wait_for_timeout(4000)
                print("address changed:", page.url != before,
                      "->", re.sub(r"\d+", "N", urlparse(page.url).path))
                for selector in ["[role=dialog]", "mat-dialog-container", ".modal", "mat-drawer",
                                 ".cdk-overlay-pane", "[class*=drawer]", "[class*=panel]"]:
                    count = page.locator(selector).count()
                    if count:
                        print(f"after click, count {selector}: {count}")
                print("--- patient file: Images section ---")
                print("found Images:", open_images_tab(page, config),
                      "->", re.sub(r"\d+", "N", urlparse(page.url).path))
                page.wait_for_timeout(3000)
                # The images part of the patient file (not the side panel with
                # the patient's photo, name and menu).
                area = page.locator(".layout-ov-page > :not(.layout-ov-page__aside)")
                if not area.count():
                    area = page.locator("mat-dialog-container, [role=dialog], main, body").first
                for selector in ["img", "canvas", "a[href]", "a[download]", "button", "mat-card, sc-card",
                                 "[class*=thumb]", "[class*=image]", "[class*=site]", "[aria-label*=download i]"]:
                    print(f"in images area, count {selector}: "
                          f"{sum(area.nth(i).locator(selector).count() for i in range(area.count()))}")
                for i in range(area.count()):
                    imgs = area.nth(i).locator("img")
                    for j in range(min(imgs.count(), 5)):
                        # Only the shape of the address: file names could include patient details.
                        src = urlparse(urljoin(page.url, imgs.nth(j).get_attribute("src") or ""))
                        parts = [x for x in src.path.split("/") if x]
                        print(f"  image address: {src.scheme}://{src.netloc}/{parts[0] if parts else ''}/..."
                              f" ({len(parts)} parts, ends {Path(src.path).suffix or 'with no extension'}"
                              f"{', has ?query' if src.query else ''})")
                select = button_named(page, config["select_text"], wait_seconds=config["ui_wait_seconds"])
                print("found Select image:", bool(select))
                if select:
                    select.click()
                    page.wait_for_timeout(2000)
                    area = images_area(page)
                    boxes = sum(area.nth(i).locator("input[type=checkbox]").count() for i in range(area.count()))
                    print("checkboxes after Select image:", boxes,
                          "| Download button:", bool(button_named(page, config["download_text"], wait_seconds=3)))
                print("--- images area ---")
                for i in range(area.count()):
                    print(area.nth(i).evaluate(DESCRIBE_JS)[:15000])
            print("===== COPY TO HERE =====\n")
        finally:
            browser.close()


def try_one(config, headed=True):
    """Download one image from the first patients in the list, as a test.

    It isn't recorded as downloaded, so the real runs still treat it normally.
    """
    username, password = get_login()
    timeout_ms = int(config["timeout_seconds"] * 1000)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(accept_downloads=True, viewport={"width": 1600, "height": 1000})
        context.set_default_timeout(timeout_ms)
        page = context.new_page()
        try:
            login(page, config, username, password)
            open_patient_list(page, config)
            wait_for_rows(page, config)
            patients = patient_links_on_page(page, config) or patients_by_clicking_rows(page, config, 1, limit=5)
            today = datetime.now().strftime("%Y-%m-%d")
            for key, name, download, dates in iter_images(page, patients, config, timeout_ms):
                files = download(f"{name} - {dates[0].isoformat() if dates else today}")
                for f in files:
                    log.info("Test download saved: %s", f)
                print(f"\nIt worked: {len(files)} file(s) saved in {output_dir(config)}")
                return
            print("\nNone of the first 5 patients had images to try.")
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


# --- main --------------------------------------------------------------------

def sync(config, headed=False, dry_run=False, mark_existing=False, catch_up_days=None):
    """Download every image not downloaded before.

    mark_existing: record everything as done without downloading.
    catch_up_days: download images dated within the last N days, and record
    older ones (and undated ones) as done without downloading them.
    """
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

            cutoff = date.today() - timedelta(days=catch_up_days) if catch_up_days is not None else None
            listed = saved = 0
            for key, name, download, dates in iter_images(page, patients, config, timeout_ms):
                listed += 1
                if key in state["downloaded"]:
                    continue
                recent = [d for d in dates if cutoff is None or d >= cutoff]
                skip = mark_existing or (cutoff is not None and not recent)
                if dry_run:
                    log.info("%s an image for %s", "Would skip" if skip else "Would download", name)
                    continue
                files = []
                if not skip:
                    taken = (recent or dates or [None])[0]
                    files = download(f"{name} - {taken.isoformat() if taken else today}")
                    for f in files:
                        log.info("Saved %s", f)
                    saved += len(files)
                state["downloaded"][key] = {
                    "patient": name,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "files": [f.name for f in files],
                }
                save_state(state)

            log.info("%d images listed", listed)
            if mark_existing:
                log.info("Marked all current images as already downloaded")
            elif catch_up_days is not None:
                log.info("Caught up: %d images from the last %d days saved in %s; older ones marked as done",
                         saved, catch_up_days, folder)
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
    parser.add_argument("--catch-up-days", type=int, metavar="N",
                        help="download images from the last N days; mark older ones as done")
    parser.add_argument("--try-one", action="store_true",
                        help="download a single image as a test (not recorded as downloaded)")
    parser.add_argument("--inspect", action="store_true",
                        help="describe the patient list page (no patient details) for troubleshooting")
    args = parser.parse_args()

    if args.set_login:
        set_login()
        return
    if args.inspect:
        setup_logging()
        inspect(load_config())
        return
    if args.try_one:
        setup_logging()
        try:
            try_one(load_config())
        except Exception:
            log.exception("Test download failed")
            sys.exit(1)
        return

    setup_logging()
    try:
        sync(load_config(), headed=args.headed, dry_run=args.dry_run, mark_existing=args.mark_existing,
             catch_up_days=args.catch_up_days)
    except SystemExit:
        raise
    except Exception:
        log.exception("Sync failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
