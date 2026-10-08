"""End-to-end tests against a small fake Thermpix site served locally."""

import http.server
import threading
from datetime import datetime
from pathlib import Path

import pytest

import thermpix_sync

PATIENTS = {"101": "Jane Citizen", "102": "John Smith", "103": "Mary Jones"}
TODAY = datetime.now().strftime("%Y-%m-%d")

LOGIN = """<html><body><a href="/login">Log in</a></body></html>"""
LOGIN_FORM = """<html><body><form method="post" action="/login">
<input type="text" name="username"><input type="password" name="password">
<button type="submit">Sign in</button></form></body></html>"""


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
            if self.path == "/":
                return self.send(dashboard() if self.logged_in() else LOGIN)
            if self.path == "/login":
                return self.send(LOGIN_FORM)
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

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            self.send_response(303)
            if "username=josh" in body and "password=secret" in body:
                self.send_header("Set-Cookie", "session=ok; Path=/")
                self.send_header("Location", "/")
            else:
                self.send_header("Location", "/login")
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
                  output_dir=str(tmp_path / "DPAs"), timeout_seconds=10, patients_url=None)
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
    with pytest.raises(RuntimeError, match="Login didn't go through"):
        thermpix_sync.sync(site["config"])
