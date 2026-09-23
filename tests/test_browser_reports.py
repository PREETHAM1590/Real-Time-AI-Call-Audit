"""Real-browser checks for safe report rendering and cohort suppression."""

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
import re
from urllib.parse import urlsplit
from contextlib import contextmanager

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class QuietHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        page_routes = {"/analyst": "/index.html", "/quality": "/quality.html", "/providers": "/providers.html"}
        path = page_routes.get(path.split("?", 1)[0], path)
        if path.startswith("/analyst-assets/"):
            path = path[len("/analyst-assets"):]
        return super().translate_path(path)

    def log_message(self, *_args):
        pass


@unittest.skipIf(sync_playwright is None, "Install `pip install -e .[browser-test]` and `python -m playwright install chromium`")
class QualityReportBrowserTests(unittest.TestCase):
    @contextmanager
    def run_report_page(self, handler):
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
                page.goto(f"http://127.0.0.1:{server.server_port}/quality.html")
                yield page
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_agent_report_shows_machine_and_reviewed_scores_and_safe_notes(self):
        data = {"items": [{
            "call_id": "call-1", "created_at": "2026-09-23T10:00:00+00:00", "processing_state": "NEEDS_REVIEW",
            "team_id": "team-a", "machine_score": 3.1, "machine_decision": "NEEDS_REVIEW", "reviewed_score": 4.0,
            "reviewed_decision": "PASS", "rubric_version": "rubric-v1", "model_artifact": "local-model-v1",
            "checklist": [{"id": "greeting", "status": "SCORED", "machine_score": 3, "reviewed_score": 4, "reason": "Good"}],
            "coaching_notes": [{"kind": "coaching", "text": "[REDACTED] <img src=x onerror=alert(1)>"}],
        }]}

        def handle(route):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

        with self.run_report_page(handle) as page:
            page.get_by_text("Your reviewed scores are loaded.").wait_for()
            text = page.locator("#own-items").inner_text()
            self.assertIn("Machine score", text)
            self.assertIn("3.1", text)
            self.assertIn("Reviewed score", text)
            self.assertIn("4", text)
            self.assertIn("[REDACTED]", text)
            self.assertIn("<img src=x onerror=alert(1)>", text)
            self.assertIn("Your reviewed scores are loaded.", page.locator("#report-status").inner_text())
            self.assertEqual(page.locator("#own-items img").count(), 0)
            self.assertFalse(page.locator("#team-report").is_visible())

    def test_team_leader_view_marks_small_cohort_suppressed(self):
        report = {"cohorts": [
            {"team_id": "team-a", "rubric_version": "r1", "model_artifact": "m1", "suppressed": False, "distinct_agents": 5, "sample_count": 12, "machine_average": 3.2, "reviewed_average": 3.5},
            {"team_id": "team-b", "rubric_version": "r1", "model_artifact": "m1", "suppressed": True},
        ]}

        def handle(route):
            path = urlsplit(route.request.url).path
            if path == "/v1/me/scores":
                route.fulfill(status=403, content_type="application/json", body='{"detail":"Agent identity required"}')
            elif path == "/v1/reports/team":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(report))
            else:
                route.fulfill(status=404, body="{}")

        with self.run_report_page(handle) as page:
            page.get_by_text("Team report loaded.").wait_for()
            text = page.locator("#team-items").inner_text()
            self.assertIn("team-a", text)
            self.assertIn("3.5", text)
            self.assertIn("team-b", text)
            self.assertIn("Fewer than five agents", text)

    def test_provider_route_navigation_and_capability_matrix_are_explicit(self):
        def handle(route):
            route.fulfill(status=404, content_type="application/json", body="{}")

        with self.run_report_page(handle) as page:
            page.goto(page.url.replace("/quality.html", "/providers"))
            page.get_by_role("heading", name="Provider coverage").wait_for()
            self.assertIn("Not connected", page.locator(".connection-state").inner_text())
            table = page.get_by_role("table", name="Public documentation signals and project qualification status")
            for header in ["Live audio", "Post-call recording", "Call events"]:
                self.assertTrue(table.get_by_role("columnheader", name=header).count())
            self.assertTrue(table.get_by_role("rowheader", name="Exotel AgentStream / Programmable Voice").count())
            exotel_events = table.get_by_role("row", name=re.compile("Exotel")).locator("td").nth(2)
            self.assertEqual(exotel_events.locator(".capability").inner_text(), "Documented")
            self.assertIn("gRPC call-leg events", exotel_events.inner_text())
            self.assertIn("project adapter and agent attribution remain unverified", exotel_events.inner_text())
            self.assertTrue(table.get_by_text("Other / custom integration").count())
            self.assertTrue(table.get_by_text("agent-leg coverage and role attribution for this audit remain unverified.").count())
            myoperator_recording = table.get_by_role("row", name=re.compile("MyOperator")).locator("td").nth(1)
            self.assertIn("valid for 24 hours", myoperator_recording.inner_text())
            self.assertIn("account-specific entitlement and runtime behavior remain unverified", myoperator_recording.inner_text())
            page.get_by_role("link", name="Quality", exact=True).click()
            page.get_by_role("heading", name="Quality reports").wait_for()
