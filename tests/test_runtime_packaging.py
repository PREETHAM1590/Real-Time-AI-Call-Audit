"""Smoke checks for the native process entrypoints and container contract."""

import importlib
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.worker import run_forever


class RuntimePackagingTests(unittest.TestCase):
    def test_asgi_entrypoint_builds_app_from_validated_environment(self):
        environment = {
            "OIDC_ISSUER": "https://issuer.example.test",
            "OIDC_AUDIENCE": "call-audit-test",
            "OIDC_PUBLIC_KEY": "test-only-key-material",
            "CSRF_SECRET": "test-only-csrf-secret-long-enough-for-settings",
            "ALLOWED_ORIGINS": '["http://localhost:3000"]',
        }
        with patch.dict(os.environ, environment, clear=False):
            sys.modules.pop("app.main", None)
            module = importlib.import_module("app.main")
            composed = module.app
            while not hasattr(composed, "routes") and hasattr(composed, "app"):
                composed = composed.app
            self.assertIn("/health", {route.path for route in composed.routes})
            self.assertIn("/providers", {route.path for route in composed.routes})
            self.assertIn("/dashboard", {route.path for route in composed.routes})
            self.assertIn("/dispositions", {route.path for route in composed.routes})
            from fastapi.testclient import TestClient

            with TestClient(module.app) as client:
                dashboard = client.get("/dashboard")
                dispositions = client.get("/dispositions")
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn("text/html", dashboard.headers["content-type"])
            self.assertIn("Overview dashboard", dashboard.text)
            self.assertEqual(dispositions.status_code, 200)
            self.assertIn("text/html", dispositions.headers["content-type"])
            self.assertIn("Disposition configuration", dispositions.text)
            sys.modules.pop("app.main", None)

    def test_worker_idle_loop_waits_then_honours_shutdown(self):
        class StopAfterWait:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, seconds):
                self.asserted_seconds = seconds
                self.stopped = True
                return True

        stop_event = StopAfterWait()
        with patch("app.worker.run_once", return_value=False) as run_once, patch("app.worker.sweep_live_transcripts"):
            run_forever("test-worker", {}, idle_poll_seconds=0.5, stop_event=stop_event)
        run_once.assert_called_once_with("test-worker", {})
        self.assertEqual(stop_event.asserted_seconds, 0.5)

    def test_worker_idle_interval_is_bounded(self):
        for invalid in (0, 0.49, 10.01, float("inf"), float("nan"), True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                run_forever("test-worker", {}, idle_poll_seconds=invalid, stop_event=threading.Event())

    def test_docker_runtime_declares_unprivileged_user_and_locked_install(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("uv sync --frozen", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn('"uvicorn", "app.main:app"', dockerfile)
        self.assertIn("COPY --from=uv-bin", dockerfile)


if __name__ == "__main__":
    unittest.main()
