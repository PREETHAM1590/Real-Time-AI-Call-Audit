"""Real-browser smoke check for the dependency-free disposition configuration page."""

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
        page_routes = {"/dispositions": "/dispositions.html"}
        path = page_routes.get(path.split("?", 1)[0], path)
        if path.startswith("/analyst-assets/"):
            path = path[len("/analyst-assets"):]
        return super().translate_path(path)

    def log_message(self, *_args):
        pass


VERSIONS = {"versions": [
    {"config_id": "retention_v1", "use_case_id": "retention", "version": 1, "content_hash": "a" * 64,
     "schema_version": "1.0.0", "created_by": "admin-a", "created_at": "2026-09-20T00:00:00+00:00",
     "active": True, "status": "ACTIVE", "approved_by": "admin-b", "active_generation": 1},
    {"config_id": "retention_v1", "use_case_id": "retention", "version": 2, "content_hash": "b" * 64,
     "schema_version": "1.0.0", "created_by": "admin-a", "created_at": "2026-09-23T00:00:00+00:00",
     "active": False, "status": "STAGED", "approved_by": None, "active_generation": 1},
]}


@unittest.skipIf(sync_playwright is None, "Install the optional browser-test extra and Chromium to run the browser check")
class DispositionsBrowserSmokeTests(unittest.TestCase):
    @contextmanager
    def run_dispositions(self, handler):
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
                page.goto(f"http://127.0.0.1:{server.server_port}/dispositions.html")
                yield page
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_lists_versions_with_status_and_lineage(self):
        def handle(route):
            path = urlsplit(route.request.url).path
            if path == "/v1/csrf":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"csrf_token": "synthetic-csrf"}))
            elif path == "/v1/disposition-configs":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(VERSIONS))
            else:
                route.fulfill(status=404, content_type="application/json", body="{}")

        with self.run_dispositions(handle) as page:
            page.get_by_text("configuration version", exact=False).first.wait_for()
            table_text = page.locator("#versions-table").inner_text()
            self.assertIn("retention_v1", table_text)
            self.assertIn("v1", table_text)
            self.assertIn("v2", table_text)
            self.assertIn("ACTIVE", table_text)
            self.assertIn("STAGED", table_text)
            self.assertIn("admin-a", table_text)
            self.assertIn("admin-b", table_text)
            self.assertIn("aaaaaaaaaaaa", table_text)

    def test_validate_shows_422_field_errors(self):
        def handle(route):
            path = urlsplit(route.request.url).path
            if path == "/v1/csrf":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"csrf_token": "synthetic-csrf"}))
            elif path == "/v1/disposition-configs":
                route.fulfill(status=200, content_type="application/json", body='{"versions":[]}')
            elif path == "/v1/disposition-configs/validate":
                route.fulfill(status=422, content_type="application/json", body=json.dumps({
                    "detail": [{"path": "questions.committed.type", "message": "must be one of noul, choice, score"}],
                }))
            else:
                route.fulfill(status=404, content_type="application/json", body="{}")

        with self.run_dispositions(handle) as page:
            page.locator("#dispositions-admin").wait_for()
            page.get_by_role("button", name="Load synthetic sample").click()
            self.assertIn("retention_v1", page.locator("#config-json").input_value())
            page.get_by_role("button", name="Validate", exact=True).click()
            page.get_by_text("Validation failed", exact=False).wait_for()
            errors_text = page.locator("#field-errors").inner_text()
            self.assertIn("questions.committed.type", errors_text)
            self.assertIn("must be one of noul, choice, score", errors_text)

    def test_approve_sends_csrf_and_reason_and_activate_handles_conflict(self):
        approve_requests = []
        activate_requests = []
        activate_conflict = True

        def handle(route):
            nonlocal activate_conflict
            path = urlsplit(route.request.url).path
            if path == "/v1/csrf":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"csrf_token": "synthetic-csrf"}))
            elif path == "/v1/disposition-configs":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"versions": [
                    {"config_id": "retention_v1", "use_case_id": "retention", "version": 2, "content_hash": "b" * 64,
                     "schema_version": "1.0.0", "created_by": "admin-a", "created_at": "2026-09-23T00:00:00+00:00",
                     "active": False, "status": "APPROVED" if approve_requests else "STAGED", "approved_by": "admin-b" if approve_requests else None,
                     "active_generation": 0},
                ]}))
            elif path == "/v1/disposition-configs/retention_v1/versions/2/approve":
                body = json.loads(route.request.post_data)
                approve_requests.append((route.request.headers.get("x-csrf-token"), body))
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"config_id": "retention_v1", "version": 2, "status": "APPROVED"}))
            elif path == "/v1/disposition-configs/retention_v1/versions/2/activate":
                activate_requests.append(json.loads(route.request.post_data))
                if activate_conflict:
                    activate_conflict = False
                    route.fulfill(status=409, content_type="application/json", body='{"detail":"Active configuration changed"}')
                else:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"config_id": "retention_v1", "version": 2, "generation": 1}))
            else:
                route.fulfill(status=404, content_type="application/json", body="{}")

        with self.run_dispositions(handle) as page:
            page.get_by_role("button", name="Approve").wait_for()
            page.on("dialog", lambda dialog: dialog.accept("Reviewed against synthetic acceptance criteria"))
            page.get_by_role("button", name="Approve").click()
            page.locator("#action-status").get_by_text("approved", exact=False).wait_for()
            self.assertEqual(len(approve_requests), 1)
            self.assertEqual(approve_requests[0][0], "synthetic-csrf")
            self.assertEqual(approve_requests[0][1]["reason"], "Reviewed against synthetic acceptance criteria")

            page.get_by_role("button", name="Activate").wait_for()
            page.get_by_role("button", name="Activate").click()
            page.locator("#action-status").get_by_text("Conflict for retention_v1", exact=False).wait_for()
            page.get_by_role("button", name="Activate").click()
            page.locator("#action-status").get_by_text("activated", exact=False).wait_for()
            self.assertEqual(len(activate_requests), 2)
