"""A credentialed crawl must capture a sign-in form that renders AFTER the network goes idle.

Live 2026-10-02: the hosted pod's crawl_url against dev.automationhq.ai/login returned ok:true
with 0 locators (twice per run, ~8.5s each) while the same call from a laptop found all 9 — the
Next.js form mounts a beat after networkidle and the capture ran in that gap. This page
reproduces that gap deterministically: nothing in the DOM until 1.5s after load, with no network
activity in between, so networkidle (500ms of quiet) fires well before the form exists.

Runs a real headless Chromium against a local server; skipped where Chromium isn't installed.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.tools import crawl_url as mod

LATE_FORM_PAGE = b"""<!doctype html>
<html><head><title>Sign in</title></head><body><div id="root"></div>
<script>
  setTimeout(function () {
    document.getElementById('root').innerHTML =
      '<form action="/home" method="get">' +
      '<input type="email" name="email" id="email" aria-label="Email">' +
      '<input type="password" name="password" id="password" aria-label="Password">' +
      '<button type="submit" id="sign-in">SIGN IN</button>' +
      '</form>';
  }, 1500);
</script></body></html>"""

HOME_PAGE = b"<!doctype html><html><head><title>Home</title></head><body><h1>Dashboard</h1></body></html>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = HOME_PAGE if self.path.startswith("/home") else LATE_FORM_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def late_form_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/login"
    server.shutdown()


async def test_credentialed_crawl_waits_for_a_late_rendering_sign_in_form(late_form_server):
    try:
        result = await mod._crawl(late_form_server, {"username": "qa@example.com", "password": "pw"},
                                  max_pages=1, hosted=False)
    except Exception as e:  # no Chromium on this machine/CI image
        pytest.skip(f"Chromium unavailable: {e}")
    if "error" in result:
        pytest.skip(f"Chromium unavailable: {result['error']}")

    login = next(p for p in result["pages"] if p["url"].endswith("/login"))
    assert login["total_valid"] >= 3, login
    found = " ".join(str(loc) for loc in login["locators"]).lower()
    assert "password" in found and "email" in found and "sign" in found
