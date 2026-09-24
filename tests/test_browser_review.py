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
    def test_live_preview_is_redacted_stale_scoped_and_refreshes_without_replacing_row(self):
        web_root = Path(__file__).resolve().parent.parent / "web"
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(web_root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        previews = ["Phone [REDACTED]", "Corrected redacted preview"]
        preview_requests = []
        preview_failure = {"value": False}
        snapshot_status = {"value": 200}
        call = {"call_key": "a" * 64, "agent_id": "agent-a", "team_id": "team-a", "state": "LIVE",
                "started_at": "2026-09-24T00:00:00Z", "stale": True, "generation": 4}

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
                    if path == "/v1/reviews/queue":
                        route.fulfill(status=200, content_type="application/json", body='{"items":[]}')
                    elif path == "/v1/live-calls":
                        route.fulfill(status=snapshot_status["value"],
                                      content_type="application/json", body=json.dumps({"items": [call]}))
                    elif path == f"/v1/live-calls/{'a' * 64}/utterances":
                        if preview_failure["value"]:
                            route.fulfill(status=503, content_type="application/json", body='{"detail":"unavailable"}')
                            return
                        index = min(len(preview_requests), len(previews) - 1)
                        preview_requests.append(index)
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({
                            "status": "LIVE", "truncated": index > 0,
                            "items": [{"id": f"{index + 1:032x}", "role": "UNKNOWN", "start_ms": 1200,
                                       "end_ms": 1800, "text_redacted": previews[index], "is_final": False}],
                        }))
                    else:
                        route.fulfill(status=404, content_type="application/json", body="{}")

                page.route("**/v1/**", handle_api)
                page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                page.get_by_text("Phone [REDACTED]").wait_for()
                self.assertIn("UNKNOWN", page.locator(".live-utterance").inner_text())
                self.assertIn("STALE", page.locator(".live-session-status").inner_text())
                self.assertIn("provisional only", page.locator(".live-transcript-status").inner_text())
                self.assertIn("post-call transcript remains authoritative", page.locator(".live-note").inner_text())
                self.assertNotIn("5551234567", page.locator("#live-calls-list").inner_text())
                row = page.locator("#live-calls-list > li").first
                row.evaluate("node => { window.liveRow = node; }")
                page.get_by_role("button", name="Refresh live calls").click()
                page.get_by_text("Corrected redacted preview").wait_for()
                self.assertIn("Older preview utterances were dropped", page.locator(".live-transcript-status").inner_text())
                self.assertTrue(row.evaluate("node => node === window.liveRow"))
                self.assertGreaterEqual(len(preview_requests), 2)
                preview_failure["value"] = True
                page.get_by_role("button", name="Refresh live calls").click()
                page.get_by_text("Live transcript unavailable", exact=False).wait_for()
                self.assertEqual(page.locator(".live-utterances li").count(), 0)
                preview_failure["value"] = False
                page.get_by_role("button", name="Refresh live calls").click()
                page.get_by_text("Corrected redacted preview").wait_for()
                snapshot_status["value"] = 503
                page.get_by_role("button", name="Refresh live calls").click()
                page.locator("#live-status").get_by_text("temporarily unavailable", exact=False).wait_for()
                self.assertEqual(page.locator(".live-utterances li").count(), 0)
                self.assertEqual(page.locator("#live-calls-list > li.live-call").count(), 1)
                self.assertIn("STALE", page.locator(".live-session-status").inner_text())
                self.assertIn("Agent agent-a", page.locator("#live-calls-list").inner_text())
                snapshot_status["value"] = 200
                page.get_by_role("button", name="Refresh live calls").click()
                page.get_by_text("Corrected redacted preview").wait_for()
                snapshot_status["value"] = 403
                page.get_by_role("button", name="Refresh live calls").click()
                page.get_by_text("Live sessions hidden", exact=False).wait_for()
                self.assertEqual(page.locator("#live-calls-list > li.live-call").count(), 0)
                self.assertNotIn("Agent agent-a", page.locator("#live-calls-list").inner_text())
                self.assertNotIn("Call key", page.locator("#live-calls-list").inner_text())
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

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
                page.wait_for_function("document.querySelector('#upload-status').classList.contains('error')")
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
        citation_quote = 'I can help. <img src=x onerror=alert(1)>'
        dimensions = [{"id": name, "status": "SCORED", "score": 3, "reason": "Supported by the final agent evidence.", "evidence": [{"utterance_id": "utterance-1", "quote": citation_quote}]} for name in dimension_ids]

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
                            "transcript": [{"id": f"utterance-{index}", "role": "AGENT", "start_ms": 1750 if index == 1 else index * 1000, "end_ms": 2500 if index == 1 else index * 1000 + 750, "text_redacted": citation_quote if index == 1 else f"Synthetic transcript line {index}.", "is_final": True} for index in range(1, 61)],
                            "findings": [{"rule_id": "disclosure", "status": "NEEDS_REVIEW", "severity": "HIGH", "evidence_ids": ["utterance-1"], "remediation": "Confirm disclosure wording."}], "disposition": {"code": "CALLBACK", "config_version": 1}, "reviews": reviews, "current_review_version": review_head,
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
                    elif path == "/v1/calls/call-1/disposition":
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({
                            "call_id": "call-1", "revision": 1, "transcript_revision": 1, "config_id": "retention_v1",
                            "config_version": 1, "config_hash": "a" * 64, "schema_version": "1.0.0",
                            "model_artifact": "local-disposition-v1", "adapter_version": "vllm-adapter-1",
                            "processing_path": "MODEL_RULE", "status": "RESOLVED", "code": "CALLBACK", "parent_code": None,
                            "confidence": 0.82, "requires_review": False, "review_reason": None,
                            "matched_rule_id": "CALLBACK", "signals": {}, "usage": {}, "created_at": "2026-09-23T00:05:00+00:00",
                        }))
                    elif path == "/v1/calls/call-1/audio-access":
                        audio_grants.append(True)
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({"url": f"/v1/calls/call-1/audio?capability=synthetic-{len(audio_grants)}", "expires_in_seconds": 60}))
                    else:
                        route.fulfill(status=404, content_type="application/json", body="{}")

                page.route("**/v1/**", handle_api)
                page.goto(f"http://127.0.0.1:{server.server_port}/index.html")
                page.get_by_text("Server order: NEEDS_REVIEW, then FAIL, then other decisions; newest calls first within each group.").wait_for()
                page.get_by_role("button", name="Call call-1, PASS, machine score 3").click()
                queue_text = page.locator("#queue-list").inner_text()
                self.assertIn("Decision: PASS", queue_text)
                self.assertIn("Processing: NEEDS_REVIEW", queue_text)
                self.assertIn("Agent: agent-a", queue_text)
                self.assertIn("Team: team-a", queue_text)
                self.assertRegex(queue_text, r"Age: \d+[mhd] old")
                disposition_text = page.locator(".disposition-card").inner_text()
                self.assertIn("CALLBACK", disposition_text)
                self.assertIn("RESOLVED", disposition_text)
                self.assertIn("MODEL_RULE", disposition_text)
                self.assertIn("0.82", disposition_text)
                self.assertIn("retention_v1", disposition_text)
                self.assertIn("local-disposition-v1", disposition_text)
                self.assertIn("vllm-adapter-1", disposition_text)
                self.assertIn("aaaaaaaaaaaa", disposition_text)
                self.assertIn("Disposition (separate from QA audit score)", page.locator("#call-detail").inner_text())
                self.assertIn("1 policy finding linked", page.locator(".transcript-row").first.inner_text())
                evidence_links = page.locator(".evidence-link")
                self.assertIn(citation_quote, evidence_links.first.inner_text())
                evidence_links.last.click()
                self.assertEqual(page.locator("#call-status").inner_text(), "Evidence selected at 0:01.")
                evidence_links.first.click()
                self.assertEqual(page.locator("#call-status").inner_text(), "Evidence selected at 0:01.")
                self.assertEqual(page.locator(".transcript-row mark").inner_text(), citation_quote)
                self.assertEqual(page.locator(".transcript-row img").count(), 0)
                page.set_viewport_size({"width": 1024, "height": 700})
                self.assertEqual(page.locator(".review-workspace").evaluate("node => getComputedStyle(node).gridTemplateColumns.split(' ').length"), 2)
                page.set_viewport_size({"width": 860, "height": 700})
                self.assertEqual(page.locator(".review-workspace").evaluate("node => getComputedStyle(node).gridTemplateColumns.split(' ').length"), 1)
                page.set_viewport_size({"width": 760, "height": 700})
                page.locator(".transcript-row").nth(30).scroll_into_view_if_needed()
                rail = page.locator(".review-rail")
                self.assertEqual(rail.evaluate("node => getComputedStyle(node).position"), "sticky")
                self.assertLessEqual(rail.bounding_box()["y"], 8)
                page.get_by_role("button", name="Enable authorised audio playback").click()
                page.locator("#call-audio").wait_for()
                self.assertIn("capability=synthetic-1", page.locator("#call-audio").get_attribute("src"))
                page.evaluate("""() => Object.defineProperty(document.querySelector('#call-audio'), 'currentTime', {configurable: true, writable: true, value: 0})""")
                evidence_links.first.click()
                self.assertEqual(page.locator("#call-audio").evaluate("player => player.currentTime"), 1.75)
                page.wait_for_function("""() => {
                    const mark = document.querySelector('.transcript-row mark')?.getBoundingClientRect();
                    const rail = document.querySelector('.review-rail')?.getBoundingClientRect();
                    return mark && rail && mark.top >= rail.bottom + 4 && mark.bottom <= innerHeight;
                }""")
                mark_box = page.locator(".transcript-row mark").bounding_box()
                rail_box = page.locator(".review-rail").bounding_box()
                self.assertGreaterEqual(mark_box["y"], rail_box["y"] + rail_box["height"] + 4)
                self.assertLessEqual(mark_box["y"] + mark_box["height"], 700)
                page.set_viewport_size({"width": 390, "height": 700})
                marker_box = page.locator(".policy-evidence-marker").first.bounding_box()
                self.assertLessEqual(marker_box["x"] + marker_box["width"], 390)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
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
                page.get_by_role("button", name="Refresh", exact=True).click()
                page.get_by_role("button", name="Call call-1, PASS, machine score 3").click()
                page.get_by_text("This audit uses superseded transcript evidence.").wait_for()
                self.assertEqual(page.get_by_label("Review reason").count(), 0)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
