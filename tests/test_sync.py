"""End-to-end tests against a small fake Thermpix site served locally."""

import http.server
import threading
from datetime import datetime
from pathlib import Path

import pytest

import thermpix_sync

PATIENTS = {"101": "Jane Citizen", "102": "John Smith"}
TODAY = datetime.now().strftime("%Y-%m-%d")

LOGIN = """<html><body><a href="/login">Log in</a></body></html>"""
LOGIN_FORM = """<html><body><form method="post" action="/login">
<input type="text" name="username"><input type="password" name="password">
<button type="submit">Sign in</button></form></body></html>"""


def dashboard(mode, patients):
    rows = []
    for pid, name in patients.items():
        if mode == "direct":
            rows.append(f'<tr><td>{name}</td><td><a href="/files/{pid}.jpg">Download</a></td></tr>')
        else:
            rows.append(f'<tr><td><a href="/patient/{pid}">{name}</a></td></tr>')
    return f"""<html><body><nav><a href="/settings">Settings</a></nav>
<div class="card"><h3>Recently Created Patients</h3><table>{''.join(rows)}</table></div>
<div class="card"><h3>Other</h3><a href="/files/other.jpg">Download</a></div></body></html>"""


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
                return self.send(dashboard(site["mode"], site["patients"]) if self.logged_in() else LOGIN)
            if self.path == "/login":
                return self.send(LOGIN_FORM)
            if not self.logged_in():
                self.send_response(403)
                return self.end_headers()
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
    state = {"mode": "direct", "patients": dict(PATIENTS), "images": {"101": ["a1"], "102": ["b1"]}}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("THERMPIX_USERNAME", "josh")
    monkeypatch.setenv("THERMPIX_PASSWORD", "secret")
    config = thermpix_sync.load_config()
    config.update(base_url=f"http://127.0.0.1:{server.server_port}/",
                  output_dir=str(tmp_path / "DPAs"), timeout_seconds=10)
    state["config"] = config
    state["out"] = tmp_path / "DPAs"
    yield state
    server.shutdown()


def files(folder):
    return sorted(p.name for p in Path(folder).iterdir())


def test_direct_downloads_only_new_images_named_by_patient(site):
    thermpix_sync.sync(site["config"])
    assert files(site["out"]) == [f"Jane Citizen - {TODAY} - 101.jpg", f"John Smith - {TODAY} - 102.jpg"]
    assert (site["out"] / f"Jane Citizen - {TODAY} - 101.jpg").read_bytes() == b"JPEGDATA-101.jpg"

    thermpix_sync.sync(site["config"])  # nothing new
    assert len(files(site["out"])) == 2

    site["patients"]["103"] = "New Patient"
    thermpix_sync.sync(site["config"])
    assert files(site["out"])[-1] == f"New Patient - {TODAY} - 103.jpg"
    assert len(files(site["out"])) == 3


def test_patient_pages_and_returning_patient(site):
    site["mode"] = "patient"
    thermpix_sync.sync(site["config"])
    assert files(site["out"]) == [
        f"Jane Citizen - {TODAY} - a1.jpg",
        f"Jane Citizen - {TODAY} - report-101.pdf",
        f"John Smith - {TODAY} - b1.jpg",
        f"John Smith - {TODAY} - report-102.pdf",
    ]

    # Jane comes back for more scans: only the new image is downloaded.
    site["images"]["101"].append("a2")
    thermpix_sync.sync(site["config"])
    assert len(files(site["out"])) == 5
    assert f"Jane Citizen - {TODAY} - a2.jpg" in files(site["out"])


def test_mark_existing_skips_current_images(site):
    thermpix_sync.sync(site["config"], mark_existing=True)
    assert files(site["out"]) == []
    site["patients"]["103"] = "New Patient"
    thermpix_sync.sync(site["config"])
    assert files(site["out"]) == [f"New Patient - {TODAY} - 103.jpg"]


def test_wrong_password(site, monkeypatch):
    monkeypatch.setenv("THERMPIX_PASSWORD", "nope")
    site["config"]["timeout_seconds"] = 3
    with pytest.raises(RuntimeError, match="Login didn't go through"):
        thermpix_sync.sync(site["config"])
