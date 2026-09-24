"""Synthetic browser coverage for Exotel admin setup and one-time secrets."""

import json
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
from urllib.parse import urlsplit

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class QuietHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path.split("?", 1)[0] == "/providers":
            path = "/providers.html"
        if path.startswith("/analyst-assets/"):
            path = path[len("/analyst-assets"):]
        return super().translate_path(path)

    def log_message(self, *_args):
        pass


@unittest.skipIf(sync_playwright is None, "Install `pip install -e .[browser-test]` and `python -m playwright install chromium`")
class ExotelAdminBrowserTests(unittest.TestCase):
    @contextmanager
    def run_page(self, handler):
        web_root = Path(__file__).resolve().parent.parent / "web"
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(web_root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except Exception as error:
                    if "Executable doesn't exist" in str(error):
                        self.skipTest("Install the Playwright Chromium binary with `python -m playwright install chromium`")
                    raise
                page = browser.new_page()
                page.route("**/v1/**", handler)
                page.goto(f"http://127.0.0.1:{server.server_port}/providers")
                yield page
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_admin_can_manage_integration_and_mappings_without_secret_persistence(self):
        integration_id = "synthetic-integration"
        mappings = []
        calls = []
        active = True

        def handle(route):
            nonlocal active
            request = route.request
            path = urlsplit(request.url).path
            calls.append((request.method, path, request.post_data))
            if path == "/v1/csrf":
                route.fulfill(status=200, content_type="application/json", body='{"csrf_token":"synthetic-csrf"}')
            elif path == "/v1/exotel-integrations" and request.method == "POST":
                route.fulfill(status=201, content_type="application/json", body=json.dumps({
                    "id": integration_id, "account_sid": "acct-demo", "username": "demo-user",
                    "password": "synthetic-one-time-password", "status": "ACTIVE",
                }))
            elif path == "/v1/exotel-integrations" and request.method == "GET":
                body = {"integrations": [{"id": integration_id, "account_sid": "acct-demo", "username": "demo-user", "status": "ACTIVE" if active else "DISABLED", "created_at": "2026-09-24T00:00:00Z"}]}
                route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
            elif path.endswith("/agents") and request.method == "GET":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"mappings": mappings}))
            elif "/agents/" in path and request.method == "PUT":
                ref = path.rsplit("/", 1)[-1]
                body = json.loads(request.post_data)
                mappings[:] = [{"agent_ref": ref, **body}]
                route.fulfill(status=200, content_type="application/json", body=json.dumps(mappings[0]))
            elif "/agents/" in path and request.method == "DELETE":
                mappings.clear()
                route.fulfill(status=204, body="")
            elif path == f"/v1/exotel-integrations/{integration_id}" and request.method == "DELETE":
                active = False
                route.fulfill(status=204, body="")
            else:
                route.fulfill(status=404, content_type="application/json", body='{"detail":"Not found"}')

        with self.run_page(handle) as page:
            page.get_by_role("heading", name="Provider coverage").wait_for()
            page.get_by_role("heading", name="Exotel setup").wait_for()
            page.get_by_label("Exotel account SID").fill("acct-demo")
            page.get_by_role("button", name="Create integration").click()
            page.get_by_text("synthetic-one-time-password").wait_for()
            self.assertIn("unidirectional Stream applet is unverified", page.locator("#exotel-admin").inner_text())
            self.assertIn("membership", page.locator("#exotel-admin").inner_text())
            self.assertNotIn("synthetic-one-time-password", page.evaluate("JSON.stringify([localStorage, sessionStorage])"))

            page.get_by_label("Exotel agent reference").fill("vendor-agent-7")
            page.get_by_label("Internal agent ID").fill("agent-demo")
            page.get_by_label("Internal team ID").fill("team-demo")
            page.get_by_role("button", name="Save mapping").click()
            page.wait_for_timeout(250)
            self.assertEqual(page.locator("#exotel-status").inner_text(), "Mapping vendor-agent-7 saved.", calls)
            self.assertIn("vendor-agent-7 maps to agent-demo in team-demo", page.locator("#exotel-mapping-list").inner_text())
            self.assertTrue(any(method == "PUT" and path.endswith("/agents/vendor-agent-7") and json.loads(body) == {"agent_id": "agent-demo", "team_id": "team-demo"} for method, path, body in calls if body))

            page.get_by_role("button", name="Remove").click()
            page.get_by_text("No agent mappings yet.").wait_for()
            page.get_by_role("button", name="Disable").click()
            page.get_by_text("acct-demo · demo-user · DISABLED").wait_for()
            self.assertFalse(page.locator("#exotel-map-form").is_visible())
            self.assertFalse(page.get_by_role("button", name="Disable").count())
            page.get_by_role("button", name="I’ve saved it · dismiss credential").click()
            self.assertNotIn("synthetic-one-time-password", page.locator("body").inner_text())

            page.reload()
            page.get_by_text("Admin setup loaded.").wait_for()
            self.assertNotIn("synthetic-one-time-password", page.locator("body").inner_text())
            self.assertNotIn("synthetic-one-time-password", page.evaluate("JSON.stringify([localStorage, sessionStorage])"))

    def test_one_time_credential_survives_a_failed_post_create_refresh(self):
        created = False

        def handle(route):
            nonlocal created
            request = route.request
            path = urlsplit(request.url).path
            if path == "/v1/csrf":
                route.fulfill(status=200, content_type="application/json", body='{"csrf_token":"synthetic-csrf"}')
            elif path == "/v1/exotel-integrations" and request.method == "POST":
                created = True
                route.fulfill(status=201, content_type="application/json", body=json.dumps({
                    "id": "new-integration", "username": "demo-user", "password": "one-time-secret",
                }))
            elif path == "/v1/exotel-integrations" and request.method == "GET" and created:
                route.fulfill(status=403, content_type="application/json", body='{"detail":"Administrator access required"}')
            elif path == "/v1/exotel-integrations":
                route.fulfill(status=200, content_type="application/json", body='{"integrations":[]}')
            else:
                route.fulfill(status=404, content_type="application/json", body='{"detail":"Not found"}')

        with self.run_page(handle) as page:
            page.get_by_role("heading", name="Exotel setup").wait_for()
            page.get_by_label("Exotel account SID").fill("acct-demo")
            page.get_by_role("button", name="Create integration").click()
            page.get_by_text("one-time-secret").wait_for()
            self.assertTrue(page.locator("#exotel-secret").is_visible())
            self.assertIn("one-time password", page.locator("#exotel-status").inner_text())

    def test_non_admin_does_not_see_setup_controls_and_provider_matrix_remains(self):
        def handle(route):
            route.fulfill(status=403, content_type="application/json", body='{"detail":"Administrator access required"}')

        with self.run_page(handle) as page:
            self.assertFalse(page.locator("#exotel-admin").is_visible())
            self.assertIn("administrator access", page.locator("#exotel-status").inner_text().lower())
            self.assertTrue(page.get_by_role("table", name="Public documentation signals and project qualification status").is_visible())


if __name__ == "__main__":
    unittest.main()
