"""Real-browser smoke check for the dependency-free analyst review page."""

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
import unittest
from urllib.parse import urlsplit

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class QuietHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path.startswith("/analyst-assets/"):
            path = path[len("/analyst-assets"):]
        return super().translate_path(path)

    def log_message(self, *_args):
        pass


@unittest.skipIf(sync_playwright is None, "Install the optional browser-test extra and Chromium to run the browser check")
class AnalystBrowserSmokeTests(unittest.TestCase):
    def test_upload_conflicts_show_distinct_recovery_messages(self):
        web_root = Path(__file__).resolve().parent.parent / "web"
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(web_root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        uploads = []

        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except Exception as error:
                    if "Executable doesn't exist" in str(error):
                        self.skipTest("Install the Playwright Chromium binary with `python -m playwright install chromium`")
                    raise
                page = browser.new_page()

                def handle_api(route):
                    path = urlsplit(route.request.url).path
                    if path == "/v1/csrf":
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({"csrf_token": "synthetic-csrf"}))
                    elif path == "/v1/reviews/queue":
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": []}))
                    elif path == "/v1/calls" and route.request.method == "POST":
                        uploads.append(route.request.headers.get("idempotency-key"))
                        detail = "External reference already exists" if b"taken-reference" in route.request.post_data_buffer else "Idempotency key conflicts with existing upload"
                        route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": detail}))
                    else:
                        route.fulfill(status=404, content_type="application/json", body="{}")

                page.route("**/v1/**", handle_api)
                page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                page.get_by_label("External reference").fill("taken-reference")
                page.get_by_label("Language").fill("en")
                page.locator("#upload-audio").set_input_files({"name": "synthetic.wav", "mimeType": "audio/wav", "buffer": b"synthetic wav fixture"})
                page.get_by_role("button", name="Upload recording").click()
                page.get_by_text("That external reference is already in use. Choose a different reference.").wait_for()

                page.get_by_label("External reference").fill("key-conflict-reference")
                page.get_by_role("button", name="Upload recording").click()
                page.get_by_text("This upload key conflicts with a prior request. Retry the original unchanged upload, or edit the form to start a new upload.").wait_for()
                self.assertEqual(len(uploads), 2)
                self.assertNotEqual(uploads[0], uploads[1])
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_upload_disables_form_until_delayed_response_and_rejects_other_extensions(self):
        web_root = Path(__file__).resolve().parent.parent / "web"
        uploads = []

        class DelayedUploadHandler(QuietHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/v1/csrf":
                    body = json.dumps({"csrf_token": "synthetic-csrf"}).encode()
                elif path == "/v1/reviews/queue":
                    body = json.dumps({"items": []}).encode()
                else:
                    return super().do_GET()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                path = urlsplit(self.path).path
                if path != "/v1/calls":
                    self.send_error(404)
                    return
                uploads.append({"headers": dict(self.headers), "body": self.rfile.read(int(self.headers.get("Content-Length", "0")))})
                time.sleep(0.5)
                body = json.dumps({"id": "delayed-call", "processing_state": "QUEUED"}).encode()
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(DelayedUploadHandler, directory=str(web_root)))
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

                page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                page.get_by_label("External reference").fill("synthetic-delayed-ref")
                page.get_by_label("Language").fill("en")
                self.assertEqual(page.get_by_label("Language").get_attribute("maxlength"), "32")
                page.locator("#upload-audio").set_input_files({"name": "synthetic.txt", "mimeType": "text/plain", "buffer": b"synthetic"})
                page.get_by_role("button", name="Upload recording").click()
                page.get_by_text("Choose a file with a .wav or .mp3 extension.").wait_for()
                self.assertEqual(uploads, [])

                page.locator("#upload-audio").set_input_files({"name": "synthetic.wav", "mimeType": "audio/wav", "buffer": b"synthetic wav fixture"})
                page.get_by_role("button", name="Upload recording").click()
                page.wait_for_function("document.querySelector('#upload-submit').disabled")
                controls = page.locator("#call-upload-form").evaluate("form => [...form.elements].map(node => [node.id, node.disabled])")
                self.assertTrue(all(disabled for _, disabled in controls), controls)
                self.assertEqual(page.locator("#upload-external-ref").input_value(), "synthetic-delayed-ref")
                page.get_by_text("Call delayed-call is queued for processing.").wait_for()
                self.assertTrue(page.locator("#call-upload-form").evaluate("form => [...form.elements].every(node => !node.disabled)"))
                self.assertEqual(page.locator("#upload-external-ref").input_value(), "")
                self.assertEqual(len(uploads), 1)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_manual_upload_multipart_csrf_queue_and_retry_key(self):
        web_root = Path(__file__).resolve().parent.parent / "web"
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(web_root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        uploads = []

        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except Exception as error:
                    if "Executable doesn't exist" in str(error):
                        self.skipTest("Install the Playwright Chromium binary with `python -m playwright install chromium`")
                    raise
                page = browser.new_page()

                def handle_api(route):
                    path = urlsplit(route.request.url).path
                    if path == "/v1/csrf":
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({"csrf_token": "synthetic-csrf"}))
                    elif path == "/v1/reviews/queue":
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": []}))
                    elif path == "/v1/calls" and route.request.method == "POST":
                        uploads.append({
                            "headers": route.request.headers,
                            "body": route.request.post_data_buffer,
                        })
                        if len(uploads) == 1:
                            route.abort("failed")
                        else:
                            route.fulfill(status=202, content_type="application/json", body=json.dumps({"id": f"synthetic-call-{len(uploads) - 1}", "processing_state": "QUEUED"}))
                    else:
                        route.fulfill(status=404, content_type="application/json", body="{}")

                page.route("**/v1/**", handle_api)
                page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                self.assertIn("exactly one server-resolved team", page.locator("#upload-title").locator("xpath=following-sibling::p").inner_text())
                page.get_by_label("External reference").fill("synthetic-reference")
                page.get_by_label("Language").fill("en")
                page.locator("#upload-audio").set_input_files({"name": "synthetic.wav", "mimeType": "audio/wav", "buffer": b"synthetic wav fixture"})
                page.locator("#upload-audio").evaluate("input => Object.defineProperty(input.files[0], 'size', { configurable: true, value: 250 * 1024 * 1024 + 1 })")
                page.get_by_role("button", name="Upload recording").click()
                page.get_by_text("The recording exceeds the 250 MiB upload limit.").wait_for()
                self.assertEqual(len(uploads), 0)

                page.locator("#upload-audio").set_input_files({"name": "synthetic.wav", "mimeType": "audio/wav", "buffer": b"synthetic wav fixture"})
                page.get_by_role("button", name="Upload recording").click()
                page.wait_for_function("document.querySelector('#upload-status').textContent.length > 0")
                self.assertTrue(page.locator("#upload-status").evaluate("node => node.classList.contains('error')"))
                self.assertEqual(len(uploads), 1)

                first_headers = uploads[0]["headers"]
                first_key = first_headers.get("idempotency-key")
                self.assertTrue(first_key)
                self.assertEqual(first_headers.get("x-csrf-token"), "synthetic-csrf")
                self.assertRegex(first_headers.get("content-type", ""), r"^multipart/form-data; boundary=")
                self.assertNotEqual(first_headers.get("content-type"), "application/json")
                first_body = uploads[0]["body"]
                self.assertIn(b'name="external_ref"', first_body)
                self.assertIn(b"synthetic-reference", first_body)
                self.assertIn(b'name="language"', first_body)
                self.assertIn(b'filename="synthetic.wav"', first_body)
                self.assertIn(b"synthetic wav fixture", first_body)

                page.get_by_role("button", name="Upload recording").click()
                page.get_by_text("Call synthetic-call-1 is queued for processing.").wait_for()
                self.assertEqual(len(uploads), 2)
                self.assertEqual(uploads[1]["headers"].get("idempotency-key"), first_key)
                self.assertEqual(uploads[1]["headers"].get("x-csrf-token"), "synthetic-csrf")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_queue_evidence_navigation_and_reasoned_review(self):
        web_root = Path(__file__).resolve().parent.parent / "web"
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(web_root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        review_requests = []
        audio_grants = []
        review_saved = False
        review_conflict = True
        review_head = 0
        superseded_mode = False
        dimension_ids = ("greeting", "listening", "resolution", "compliance", "clarity", "objection", "closing")
        dimensions = [{"id": name, "status": "SCORED", "score": 3, "reason": "Supported by the final agent evidence.", "evidence": [{"utterance_id": "utterance-1", "quote": "I can help."}]} for name in dimension_ids]

        try:
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except Exception as error:
                    if "Executable doesn't exist" in str(error):
                        self.skipTest("Install the Playwright Chromium binary with `python -m playwright install chromium`")
                    raise
                page = browser.new_page()

                def handle_api(route):
                    nonlocal review_saved, review_conflict, review_head
                    path = urlsplit(route.request.url).path
                    if path == "/v1/csrf":
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({"csrf_token": "synthetic-csrf"}))
                    elif path.startswith("/v1/reviews/queue"):
                        items = [] if review_saved and not superseded_mode else [{"call_id": "call-1", "audit_id": "audit-1", "machine_decision": "PASS", "machine_score": 3.0, "processing_state": "NEEDS_REVIEW", "agent_id": "agent-a", "team_id": "team-a", "created_at": "2026-09-23T00:00:00+00:00", "transcript_revision": 1, "audit_revision": 1}]
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": items}))
                    elif path == "/v1/calls/call-1":
                        reviews = [{"id": "review-1", "version": 1, "action": "ACCEPT", "effective_score": 3.0, "effective_decision": "PASS", "reason": "Synthetic evidence checked"}] if review_head else []
                        detail = {
                            "call": {"id": "call-1", "agent_id": "agent-a", "team_id": "team-a", "processing_state": "NEEDS_REVIEW", "language": "en", "created_at": "2026-09-23T00:00:00+00:00", "transcript_revision": 1},
                            "audit": {"id": "audit-1", "revision": 1, "transcript_revision": 1, "rubric_version": "synthetic-v1", "machine_score": 3.0, "machine_decision": "PASS", "review_reason": None, "dimensions": dimensions, "superseded": superseded_mode},
                            "transcript": [{"id": "utterance-1", "role": "AGENT", "start_ms": 1750, "end_ms": 2500, "text_redacted": "I can help.", "is_final": True}],
                            "findings": [], "disposition": {"code": "CALLBACK", "config_version": 1}, "reviews": reviews, "current_review_version": review_head,
                        }
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))
                    elif path == "/v1/audits/audit-1/reviews":
                        review_requests.append(json.loads(route.request.post_data))
                        if review_conflict:
                            review_conflict = False
                            review_head = 1
                            route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": "A newer review exists; reload before saving"}))
                            return
                        review_saved = True
                        review_head += 1
                        route.fulfill(status=201, content_type="application/json", body=json.dumps({"version": 1, "action": "ACCEPT"}))
                    elif path == "/v1/calls/call-1/audio-access":
                        audio_grants.append(True)
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({"url": f"/v1/calls/call-1/audio?capability=synthetic-{len(audio_grants)}", "expires_in_seconds": 60}))
                    else:
                        route.fulfill(status=404, content_type="application/json", body="{}")

                page.route("**/v1/**", handle_api)
                page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                page.get_by_role("button", name="Call call-1, PASS, machine score 3").click()
                page.get_by_role("button", name="Open AGENT evidence at 0:01").first.click()
                self.assertEqual(page.locator("#call-status").inner_text(), "Evidence selected at 0:01.")
                page.get_by_role("button", name="Enable authorised audio playback").click()
                page.locator("#call-audio").wait_for()
                self.assertIn("capability=synthetic-1", page.locator("#call-audio").get_attribute("src"))
                page.evaluate("""() => Object.defineProperty(document.querySelector('#call-audio'), 'currentTime', {configurable: true, writable: true, value: 0})""")
                page.get_by_role("button", name="Open AGENT evidence at 0:01").first.click()
                self.assertEqual(page.locator("#call-audio").evaluate("player => player.currentTime"), 1.75)
                page.get_by_role("button", name="Renew audio access").click()
                page.wait_for_function("document.querySelector('#call-audio').src.includes('synthetic-2')")
                self.assertEqual(len(audio_grants), 2)
                page.get_by_label("Review reason").fill("Synthetic evidence checked")
                page.get_by_role("button", name="Accept current score").click()
                page.get_by_text("The review changed. Reload the latest call evidence before saving again.").wait_for()
                page.get_by_label("Review reason").fill("Confirmed after conflict reload")
                page.get_by_role("button", name="Accept current score").click()
                page.get_by_text("Review saved.").wait_for()
                self.assertEqual(len(review_requests), 2)
                self.assertEqual(review_requests[0]["action"], "ACCEPT")
                self.assertEqual(review_requests[0]["base_review_version"], 0)
                self.assertEqual(review_requests[1]["base_review_version"], 1)
                self.assertEqual(review_requests[1]["reason"], "Confirmed after conflict reload")
                self.assertEqual(page.get_by_label("Review reason").get_attribute("required"), "")
                self.assertIn("Human review history", page.locator("#call-detail").inner_text())
                superseded_mode = True
                page.get_by_role("button", name="Refresh").click()
                page.get_by_role("button", name="Call call-1, PASS, machine score 3").click()
                page.get_by_text("This audit uses superseded transcript evidence.").wait_for()
                self.assertEqual(page.get_by_label("Review reason").count(), 0)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
