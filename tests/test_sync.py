"""End-to-end tests against a small fake Thermpix site served locally."""

import http.server
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import thermpix_sync

SPA_HTML = (Path(__file__).parent / "spa_site.html").read_text()
PATIENTS = {"101": "Jane Citizen", "102": "John Smith", "103": "Mary Jones"}
TODAY = datetime.now().strftime("%Y-%m-%d")

LOGIN = """<html><body><a href="/login">Log in</a></body></html>"""
LOGIN_FORM = """<html><body><form method="post" action="/login">
<input type="text" name="username"><input type="password" name="password">
<button type="submit">Sign in</button></form>{error}</body></html>"""


MENU = """<nav><a href="/">Dashboard</a> <a href="/entities">Entities</a> <a href="/clinics">Clinics</a>
<a href="/users">Users</a> <span onclick="location='/patients'">Patients</span>
<a href="/devices">Devices</a></nav>"""
PER_PAGE = 2


def dashboard():
    return f"<html><body>{MENU}<h3>Recently Created Patients</h3></body></html>"


def patient_rows(patients, page):
    """A list whose rows open the patient when clicked, with no links."""
    ids = list(patients)
    chunk = ids[(page - 1) * PER_PAGE: page * PER_PAGE]
    rows = "".join(
        f'<tr onclick="location=\'/patient/{pid}\'"><td>{patients[pid]}</td><td>01/01/1980</td></tr>'
        for pid in chunk
    )
    more = page * PER_PAGE < len(ids)
    nxt = (f'<button aria-label="Next page" onclick="location=\'/rows?page={page + 1}\'">&gt;</button>' if more
           else '<button aria-label="Next page" disabled>&gt;</button>')
    return (f"<html><body>{MENU}<table><thead><tr><th>Name</th><th>DOB</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>{nxt}</body></html>")


def patient_list(patients, page):
    ids = list(patients)
    chunk = ids[(page - 1) * PER_PAGE: page * PER_PAGE]
    rows = "".join(
        f'<tr><td><a href="/patient/{pid}">{patients[pid]}</a></td><td><a href="/patient/{pid}/edit">Edit</a></td></tr>'
        for pid in chunk
    )
    more = page * PER_PAGE < len(ids)
    nxt = f'<a href="/patients?page={page + 1}">Next</a>' if more else '<a class="disabled">Next</a>'
    return f"<html><body>{MENU}<table><tr><th>Name</th><th></th></tr>{rows}</table>{nxt}</body></html>"


def make_handler(site):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, ctype="text/html", headers=None):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def logged_in(self):
            return "session=ok" in (self.headers.get("Cookie") or "")

        def do_GET(self):
            if site.get("spa"):
                return self.spa_get()
            if self.path == "/":
                return self.send(dashboard() if self.logged_in() else LOGIN)
            if self.path.startswith("/login"):
                error = '<div role="alert">Invalid username or password</div>' if "failed" in self.path else ""
                return self.send(LOGIN_FORM.format(error=error))
            if not self.logged_in():
                self.send_response(403)
                return self.end_headers()
            if self.path.startswith("/rows"):
                page = int(self.path.split("page=")[1]) if "page=" in self.path else 1
                return self.send(patient_rows(site["patients"], page))
            if self.path.startswith("/patients"):
                page = int(self.path.split("page=")[1]) if "page=" in self.path else 1
                return self.send(patient_list(site["patients"], page))
            if self.path.startswith("/files/"):
                name = self.path.rsplit("/", 1)[1]
                return self.send(b"JPEGDATA-" + name.encode(), "image/jpeg")
            if self.path.startswith("/patient/"):
                pid = self.path.rsplit("/", 1)[1]
                links = "".join(
                    f'<li>Scan {img} <a href="/files/{img}.jpg">Download</a></li>'
                    for img in site["images"].get(pid, [])
                )
                return self.send(
                    f'<html><body><h1>{site["patients"][pid]}</h1><ul>{links}</ul>'
                    f'<button onclick="location=\'/export/{pid}\'">Download report</button></body></html>'
                )
            if self.path.startswith("/export/"):
                pid = self.path.rsplit("/", 1)[1]
                return self.send(b"PDF-" + pid.encode(), "application/pdf",
                                 {"Content-Disposition": f'attachment; filename="report-{pid}.pdf"'})
            self.send_response(404)
            self.end_headers()

        def spa_get(self):
            if self.path.startswith("/files/"):
                name = self.path.rsplit("/", 1)[1]
                return self.send(b"JPEGDATA-" + name.encode(), "image/jpeg")
            if self.path == "/api/data":
                if self.headers.get("Authorization") != "tok":
                    self.send_response(401)
                    return self.end_headers()
                return self.send(json.dumps({"patients": site["patients"], "images": site["images"],
                                             "dates": site.get("dates", {}),
                                             "today": datetime.now().strftime("%d/%m/%Y")}),
                                 "application/json")
            return self.send(SPA_HTML)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            if site.get("spa"):
                if self.path == "/api/archive":
                    site["archived"] = True
                    return self.send("{}", "application/json")
                if "username=josh" in body and "password=secret" in body:
                    return self.send('{"token": "tok"}', "application/json")
                self.send_response(401)
                return self.end_headers()
            self.send_response(303)
            if "username=josh" in body and "password=secret" in body:
                self.send_header("Set-Cookie", "session=ok; Path=/")
                self.send_header("Location", "/")
            else:
                self.send_header("Location", "/login?failed=1")
            self.end_headers()

    return Handler


@pytest.fixture
def site(tmp_path, monkeypatch):
    state = {"patients": dict(PATIENTS), "images": {"101": ["a1"], "102": ["b1"], "103": ["c1"]}}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("THERMPIX_USERNAME", "josh")
    monkeypatch.setenv("THERMPIX_PASSWORD", "secret")
    config = thermpix_sync.load_config()
    config.update(base_url=f"http://127.0.0.1:{server.server_port}/",
                  output_dir=str(tmp_path / "DPAs"), timeout_seconds=10, patients_url=None,
                  ui_wait_seconds=2)
    state["config"] = config
    state["out"] = tmp_path / "DPAs"
    yield state
    server.shutdown()


def files(folder):
    return sorted(p.name for p in Path(folder).iterdir())


def test_downloads_every_patient_across_pages(site):
    thermpix_sync.sync(site["config"])
    assert files(site["out"]) == [
        f"Jane Citizen - {TODAY} - a1.jpg",
        f"Jane Citizen - {TODAY} - report-101.pdf",
        f"John Smith - {TODAY} - b1.jpg",
        f"John Smith - {TODAY} - report-102.pdf",
        f"Mary Jones - {TODAY} - c1.jpg",
        f"Mary Jones - {TODAY} - report-103.pdf",
    ]
    assert (site["out"] / f"Jane Citizen - {TODAY} - a1.jpg").read_bytes() == b"JPEGDATA-a1.jpg"

    thermpix_sync.sync(site["config"])  # nothing new
    assert len(files(site["out"])) == 6


def test_clickable_rows_without_links(site):
    site["config"]["patients_url"] = "/rows"
    thermpix_sync.sync(site["config"])
    assert len(files(site["out"])) == 6
    assert f"Mary Jones - {TODAY} - c1.jpg" in files(site["out"])


def test_web_app_that_logs_out_on_page_load(site, caplog):
    """Like the real Thermpix: typing an address logs you out, so it must click through."""
    site["spa"] = True
    # The menu link doesn't open the list (like the real run), so it must notice it's
    # still on the dashboard and open the full list directly.
    site["config"]["patients_url"] = "/patients/patients"
    site["images"]["101"] = ["a0", "a1"]  # two new images for one patient in one run
    thermpix_sync.sync(site["config"])
    assert files(site["out"]) == [
        f"Jane Citizen - {TODAY} - a0.jpg",
        f"Jane Citizen - {TODAY} - a1.jpg",
        f"John Smith - {TODAY} - b1.jpg",
        f"Mary Jones - {TODAY} - c1.jpg",
    ]
    site["images"]["103"].append("c2")
    thermpix_sync.sync(site["config"])
    assert len(files(site["out"])) == 5
    assert not site.get("archived"), "the Archive button must never be pressed"


def test_catch_up_last_10_days(site):
    site["spa"] = True
    site["config"]["patients_url"] = "/patients/patients"
    thermpix_sync.DATE_ORDER["order"] = None

    def ago(days, fmt="%d/%m/%Y"):
        return (datetime.now() - timedelta(days=days)).strftime(fmt)

    site["images"] = {"101": ["a1"], "102": ["b1", "b2"], "103": ["c1"]}
    site["dates"] = {"a1": ago(0), "b1": ago(30), "b2": ago(40), "c1": ago(5)}
    thermpix_sync.sync(site["config"], catch_up_days=10)
    assert files(site["out"]) == [
        f"Jane Citizen - {ago(0, '%Y-%m-%d')} - a1.jpg",
        f"Mary Jones - {ago(5, '%Y-%m-%d')} - c1.jpg",
    ]
    # The older images were recorded, so the daily run only fetches new ones.
    site["images"]["102"].append("b3")
    thermpix_sync.sync(site["config"])
    assert f"John Smith - {ago(0, '%Y-%m-%d')} - b3.jpg" in files(site["out"])
    assert len(files(site["out"])) == 3
    assert not site.get("archived")


def test_catch_up_after_images_were_only_marked(site):
    """If an earlier run only recorded images as done, a catch-up still saves the recent ones."""
    site["spa"] = True
    site["config"]["patients_url"] = "/patients/patients"
    thermpix_sync.DATE_ORDER["order"] = None
    old = (datetime.now() - timedelta(days=30)).strftime("%Y/%m/%d")
    site["dates"] = {"b1": old}
    thermpix_sync.sync(site["config"], mark_existing=True)
    assert files(site["out"]) == []
    thermpix_sync.sync(site["config"], catch_up_days=10)
    assert files(site["out"]) == [f"Jane Citizen - {TODAY} - a1.jpg", f"Mary Jones - {TODAY} - c1.jpg"]
    thermpix_sync.sync(site["config"], catch_up_days=10)  # running it again changes nothing
    assert len(files(site["out"])) == 2
    # A saved file that has gone missing from the folder is fetched again by a catch-up.
    (site["out"] / f"Mary Jones - {TODAY} - c1.jpg").unlink()
    thermpix_sync.sync(site["config"], catch_up_days=10)
    assert f"Mary Jones - {TODAY} - c1.jpg" in files(site["out"])


def test_same_person():
    assert thermpix_sync.same_person("Jane Citizen", "Citizen, Jane")
    assert thermpix_sync.same_person("Jane Citizen", "Mrs Jane Citizen")
    assert not thermpix_sync.same_person("John Smith", "Jane Citizen")


def test_yesterday_only(site):
    """The manual "last night" run saves only yesterday's images and leaves the rest alone."""
    site["spa"] = True
    site["config"]["patients_url"] = "/patients/patients"
    thermpix_sync.DATE_ORDER["order"] = None

    def ago(days, fmt="%d/%m/%Y"):
        return (datetime.now() - timedelta(days=days)).strftime(fmt)

    site["images"] = {"101": ["a1"], "102": ["b1"], "103": ["c1"]}
    site["dates"] = {"a1": ago(1), "b1": ago(0), "c1": ago(3)}
    yesterday = (datetime.now() - timedelta(days=1)).date()
    thermpix_sync.sync(site["config"], only_day=yesterday)
    assert files(site["out"]) == [f"Jane Citizen - {ago(1, '%Y-%m-%d')} - a1.jpg"]
    thermpix_sync.sync(site["config"], only_day=yesterday)  # again: nothing new
    assert len(files(site["out"])) == 1
    # Today's and older images weren't recorded, so the normal run still gets them.
    thermpix_sync.sync(site["config"])
    assert len(files(site["out"])) == 3


def test_dates():
    thermpix_sync.DATE_ORDER["order"] = None
    from datetime import date
    assert thermpix_sync.possible_dates("2026-10-03") == [date(2026, 10, 3)]
    assert thermpix_sync.possible_dates("Oct 3, 2026") == [date(2026, 10, 3)]
    assert thermpix_sync.possible_dates("2026/10/03") == [date(2026, 10, 3)]
    assert thermpix_sync.possible_dates("25-09-2026") == [date(2026, 9, 25)]
    assert thermpix_sync.possible_dates("3 October 2026") == [date(2026, 10, 3)]
    assert thermpix_sync.possible_dates("03/10/2026") == [date(2026, 10, 3), date(2026, 3, 10)]
    thermpix_sync.learn_date_order(["25/09/2026"])
    assert thermpix_sync.possible_dates("03/10/2026") == [date(2026, 10, 3)]
    thermpix_sync.DATE_ORDER["order"] = None


def test_try_one_downloads_a_single_image(site):
    site["spa"] = True
    site["config"]["patients_url"] = "/patients/patients"
    thermpix_sync.try_one(site["config"], headed=False)
    assert files(site["out"]) == [f"Jane Citizen - {TODAY} - a1.jpg"]
    assert not site.get("archived")


def test_web_app_wrong_password(site, monkeypatch):
    site["spa"] = True
    monkeypatch.setenv("THERMPIX_PASSWORD", "nope")
    site["config"]["timeout_seconds"] = 3
    with pytest.raises(RuntimeError, match="Invalid username or password"):
        thermpix_sync.sync(site["config"])


def test_returning_patient_gets_only_new_images(site):
    thermpix_sync.sync(site["config"])
    # Mary (on page 2 of the list) comes back for more scans.
    site["images"]["103"].append("c2")
    thermpix_sync.sync(site["config"])
    assert len(files(site["out"])) == 7
    assert f"Mary Jones - {TODAY} - c2.jpg" in files(site["out"])


def test_mark_existing_skips_current_images(site):
    thermpix_sync.sync(site["config"], mark_existing=True)
    assert files(site["out"]) == []
    site["images"]["101"].append("a2")
    site["patients"]["104"] = "New Patient"
    site["images"]["104"] = ["d1"]
    thermpix_sync.sync(site["config"])
    assert files(site["out"]) == [
        f"Jane Citizen - {TODAY} - a2.jpg",
        f"New Patient - {TODAY} - d1.jpg",
        f"New Patient - {TODAY} - report-104.pdf",
    ]


def test_wrong_password(site, monkeypatch):
    monkeypatch.setenv("THERMPIX_PASSWORD", "nope")
    site["config"]["timeout_seconds"] = 3
    with pytest.raises(RuntimeError, match="Thermpix says: Invalid username or password"):
        thermpix_sync.sync(site["config"])


def test_inspect_hides_patient_names(site, capsys):
    site["spa"] = True
    site["config"]["patients_url"] = "/patients/patients"
    thermpix_sync.inspect(site["config"], headed=False)
    out = capsys.readouterr().out
    assert "patient row 1" in out and "address changed: True" in out
    assert "Last name" in out and "Birthday" in out  # column headings are shown
    assert 'aria-label="Details"' in out and "found Images: True" in out
    assert "found Select image: True" in out and "checkboxes after Select image: 1 | Download button: True" in out
    for name in ["Jane", "Citizen", "John", "Smith", "Mary", "Jones"]:
        assert name not in out


class FakeSMTP:
    sent = []

    def __init__(self, server, port, timeout=None):
        self.server = server

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        assert password == "app-pass"

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def mail(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(thermpix_sync.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("THERMPIX_SMTP_SENDER", "clinic@thebalancedchiro.com.au")
    monkeypatch.setenv("THERMPIX_SMTP_PASSWORD", "app-pass")
    monkeypatch.setenv("THERMPIX_SMTP_SERVER", "smtp.gmail.com")
    return FakeSMTP.sent


def test_email_report_lists_saved_images(site, mail):
    site["spa"] = True
    site["config"]["patients_url"] = "/patients/patients"
    report = thermpix_sync.new_report("daily sync")
    thermpix_sync.sync(site["config"], report=report)
    thermpix_sync.send_report(site["config"], report)
    (msg,) = mail
    assert msg["To"] == "yandina@thebalancedchiro.com.au"
    assert msg["Subject"].startswith("Thermpix sync: 3 new images")
    body = msg.get_content()
    assert "Result: OK" in body and "Patients checked: 3" in body
    assert f"Mary Jones - {TODAY} - c1.jpg" in body


def test_email_report_on_failure(site, mail, monkeypatch):
    site["spa"] = True
    monkeypatch.setenv("THERMPIX_PASSWORD", "nope")
    site["config"]["timeout_seconds"] = 3
    report = thermpix_sync.new_report("daily sync")
    with pytest.raises(RuntimeError):
        thermpix_sync.sync(site["config"], report=report)
    report["error"] = "Login didn't go through"
    thermpix_sync.send_report(site["config"], report)
    (msg,) = mail
    assert "FAILED" in msg["Subject"]
    assert "Login didn't go through" in msg.get_body(('plain',)).get_content()
    assert [a.get_filename() for a in msg.iter_attachments()][0].startswith("error-")


def test_no_email_when_not_set_up(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("THERMPIX_SMTP_SENDER", raising=False)
    monkeypatch.setattr(thermpix_sync.keyring, "get_password", lambda *a: None)
    assert thermpix_sync.send_email(thermpix_sync.load_config(), "s", "b") is False


# --- sending to Practice Software ---------------------------------------------

def make_practice_handler(practice):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            if practice["down"]:
                return self.reply(503, {"error": "Down for maintenance"})
            if self.headers.get("Authorization") != "Bearer ps_test_key":
                return self.reply(401, {"error": "That connection key isn't working."})
            if self.path == "/api/thermpix/check":
                return self.reply(200, {"ok": True})
            if self.path == "/api/thermpix/start":
                if body["source_key"] in practice["filed"]:
                    return self.reply(200, {"already": True})
                path = f"files/{len(practice['uploads'])}.jpg"
                url = f"http://127.0.0.1:{self.server.server_port}/upload/{path}"
                return self.reply(200, {"already": False, "path": path, "upload_url": url})
            if self.path == "/api/thermpix/finish":
                assert body["path"] in practice["uploads"]
                practice["filed"][body["source_key"]] = body
                return self.reply(200, {"filed": body["name"] != "Mary Jones", "already": False})
            self.reply(404, {"error": "Not found"})

        def do_PUT(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            practice["uploads"][self.path.removeprefix("/upload/")] = data
            self.send_response(200)
            self.end_headers()

    return Handler


@pytest.fixture
def practice(monkeypatch):
    state = {"down": False, "uploads": {}, "filed": {}}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_practice_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("PRACTICE_SOFTWARE_URL", f"http://127.0.0.1:{server.server_port}/")
    monkeypatch.setenv("PRACTICE_SOFTWARE_KEY", "ps_test_key")
    yield state
    server.shutdown()


def test_new_images_are_sent_to_practice_software(site, practice):
    report = thermpix_sync.new_report("daily sync")
    thermpix_sync.sync(site["config"], report=report)
    thermpix_sync.send_pending_to_practice(report)
    sent = {v["name"] for v in practice["filed"].values()}
    assert sent == {"Jane Citizen", "John Smith", "Mary Jones"}
    assert len(practice["filed"]) == 6
    assert all(v["taken_on"] == TODAY for v in practice["filed"].values())
    assert b"JPEGDATA-a1.jpg" in practice["uploads"].values()
    assert report["sent"] == {"filed": 4, "to_file": 2, "already": 0} and report["waiting"] == 0
    assert thermpix_sync.load_state()["practice_pending"] == []
    # The DPAs folder still gets every file, as before.
    assert len(files(site["out"])) == 6


def test_images_wait_and_are_sent_next_time_when_practice_software_is_down(site, practice):
    practice["down"] = True
    report = thermpix_sync.new_report("daily sync")
    thermpix_sync.sync(site["config"], report=report)
    thermpix_sync.send_pending_to_practice(report)
    assert report["waiting"] == 6 and "maintenance" in report["send_error"]
    assert practice["filed"] == {}

    practice["down"] = False
    later = thermpix_sync.new_report("daily sync")
    thermpix_sync.send_pending_to_practice(later)
    assert len(practice["filed"]) == 6 and later["waiting"] == 0


def test_wrong_key_keeps_images_waiting(site, practice, monkeypatch, mail):
    monkeypatch.setenv("PRACTICE_SOFTWARE_KEY", "ps_wrong")
    report = thermpix_sync.new_report("daily sync")
    thermpix_sync.sync(site["config"], report=report)
    thermpix_sync.send_pending_to_practice(report)
    assert report["waiting"] == 6 and "connection key" in report["send_error"]
    thermpix_sync.send_report(site["config"], report)
    body = mail[0].get_content()
    assert "Waiting to send (tried again next run): 6" in body and "connection key" in body


def test_nothing_waits_when_not_connected(site, monkeypatch):
    monkeypatch.delenv("PRACTICE_SOFTWARE_URL", raising=False)
    monkeypatch.setattr(thermpix_sync.keyring, "get_password", lambda *a: None)
    thermpix_sync.sync(site["config"])
    assert thermpix_sync.load_state().get("practice_pending") is None
