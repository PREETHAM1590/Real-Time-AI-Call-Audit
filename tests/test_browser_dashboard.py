"""Real-browser smoke check for the dependency-free overview dashboard page."""

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
        page_routes = {"/dashboard": "/dashboard.html"}
        path = page_routes.get(path.split("?", 1)[0], path)
        if path.startswith("/analyst-assets/"):
            path = path[len("/analyst-assets"):]
        return super().translate_path(path)

    def log_message(self, *_args):
        pass


@unittest.skipIf(sync_playwright is None, "Install the optional browser-test extra and Chromium to run the browser check")
class DashboardBrowserSmokeTests(unittest.TestCase):
    @contextmanager
    def run_dashboard(self, handler):
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
                page.goto(f"http://127.0.0.1:{server.server_port}/dashboard.html")
                yield page
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_dashboard_renders_kpis_from_authorised_endpoints(self):
        def handle(route):
            path = urlsplit(route.request.url).path
            if path == "/v1/operations/summary":
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"pending_jobs": 3, "oldest_pending_age_seconds": 125, "incomplete_calls": 7}))
            elif path == "/v1/reviews/queue":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": [
                    {"call_id": "call-1", "audit_id": "audit-1", "machine_decision": "NEEDS_REVIEW", "machine_score": None,
                     "processing_state": "NEEDS_REVIEW", "agent_id": "agent-a", "team_id": "team-a", "created_at": "2026-09-23T00:00:00+00:00"},
                ]}))
            elif path == "/v1/live-calls":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": [
                    {"call_key": "a" * 64, "agent_id": "agent-a", "team_id": "team-a", "state": "LIVE", "started_at": "2026-09-24T00:00:00Z", "stale": False, "generation": 1},
                ]}))
            elif path == "/v1/disposition-configs":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"versions": [
                    {"config_id": "retention_v1", "use_case_id": "retention", "version": 1, "content_hash": "a" * 64,
                     "schema_version": "1.0.0", "created_by": "admin-a", "created_at": "2026-09-23T00:00:00+00:00",
                     "active": True, "status": "ACTIVE", "approved_by": "admin-b", "active_generation": 1},
                ]}))
            elif path == "/v1/me/scores":
                route.fulfill(status=403, content_type="application/json", body='{"detail":"Agent identity required"}')
            elif path == "/v1/reports/team":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"cohorts": [
                    {"team_id": "team-a", "rubric_version": "r1", "model_artifact": "m1", "suppressed": False,
                     "distinct_agents": 6, "sample_count": 20, "machine_average": 3.4, "reviewed_average": 3.6},
                ]}))
            else:
                route.fulfill(status=404, content_type="application/json", body="{}")

        with self.run_dashboard(handle) as page:
            page.get_by_text("Operations summary loaded.").wait_for()
            kpi_text = page.locator("#kpi-tiles").inner_text()
            self.assertIn("3", kpi_text)
            self.assertIn("7", kpi_text)
            page.get_by_text("call awaiting review", exact=False).wait_for()
            self.assertIn("call-1", page.locator("#queue-panel-body").inner_text())
            page.get_by_text("live or recently changed session", exact=False).wait_for()
            page.get_by_text("configuration version", exact=False).first.wait_for()
            page.locator("#scores-panel-status").get_by_text("team cohort", exact=False).wait_for()
            self.assertIn("team-a", page.locator("#scores-panel-body").inner_text())
            capability_text = page.locator("#capability-list").inner_text()
            self.assertIn("Live media provider adapters", capability_text)
            self.assertIn("Sentiment model", capability_text)
            self.assertIn("Model quality and latency", capability_text)

    def test_failed_endpoint_shows_failure_state_not_zeros(self):
        def handle(route):
            path = urlsplit(route.request.url).path
            if path == "/v1/operations/summary":
                route.fulfill(status=503, content_type="application/json", body='{"detail":"Operations summary unavailable"}')
            elif path == "/v1/reviews/queue":
                route.fulfill(status=200, content_type="application/json", body='{"items":[]}')
            elif path == "/v1/live-calls":
                route.fulfill(status=200, content_type="application/json", body='{"items":[]}')
            elif path == "/v1/disposition-configs":
                route.fulfill(status=200, content_type="application/json", body='{"versions":[]}')
            elif path == "/v1/me/scores":
                route.fulfill(status=200, content_type="application/json", body='{"items":[]}')
            else:
                route.fulfill(status=404, content_type="application/json", body="{}")

        with self.run_dashboard(handle) as page:
            page.get_by_text("Operations summary failed to load", exact=False).wait_for()
            kpi_text = page.locator("#kpi-tiles").inner_text()
            self.assertIn("Unavailable", kpi_text)
            self.assertNotIn(">0<", kpi_text)
            self.assertEqual(page.locator(".kpi-failed").count(), 3)

    def test_forbidden_panel_shows_role_message(self):
        def handle(route):
            path = urlsplit(route.request.url).path
            if path == "/v1/operations/summary":
                route.fulfill(status=403, content_type="application/json", body='{"detail":"Administrator role required"}')
            elif path == "/v1/reviews/queue":
                route.fulfill(status=403, content_type="application/json", body='{"detail":"QA analyst permission required"}')
            elif path == "/v1/live-calls":
                route.fulfill(status=200, content_type="application/json", body='{"items":[]}')
            elif path == "/v1/disposition-configs":
                route.fulfill(status=403, content_type="application/json", body='{"detail":"Administrator role required"}')
            elif path == "/v1/me/scores":
                route.fulfill(status=403, content_type="application/json", body='{"detail":"Agent identity required"}')
            elif path == "/v1/reports/team":
                route.fulfill(status=403, content_type="application/json", body='{"detail":"Team leader membership required"}')
            else:
                route.fulfill(status=404, content_type="application/json", body="{}")

        with self.run_dashboard(handle) as page:
            page.get_by_text("Not available for your role", exact=False).first.wait_for()
            self.assertIn("Not available for your role", page.locator("#kpi-status").inner_text())
            self.assertIn("Not available for your role", page.locator("#queue-panel-status").inner_text())
            self.assertIn("Not available for your role", page.locator("#disposition-panel-status").inner_text())
            self.assertIn("Not available for your role", page.locator("#scores-panel-status").inner_text())
