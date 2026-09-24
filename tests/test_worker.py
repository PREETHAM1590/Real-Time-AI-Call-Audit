import unittest
import logging
import threading
import uuid
from unittest.mock import patch

from app.worker import run_forever, run_once


class WorkerTests(unittest.TestCase):
    def test_expired_live_transcript_sweeper_runs_during_long_worker_call(self):
        stop_event = threading.Event()
        swept = threading.Event()

        def worker_call(*_args):
            self.assertTrue(swept.wait(1))
            stop_event.set()
            return True

        with patch("app.worker.sweep_live_transcripts", side_effect=swept.set), patch("app.worker.run_once", side_effect=worker_call):
            run_forever("worker-a", {}, stop_event=stop_event)
        self.assertTrue(swept.is_set())

    @patch("app.worker.defer_job", return_value=True)
    @patch("app.worker.claim_job", return_value=None)
    @patch("app.worker.connect")
    def test_worker_does_not_claim_stages_without_a_handler(self, connect, claim, defer):
        connection = connect.return_value.__enter__.return_value
        self.assertFalse(run_once("worker-a"))
        claim.assert_called_once_with(connection, "worker-a", 60, ())
        defer.assert_not_called()

    @patch("app.worker.finish_job", return_value=True)
    @patch("app.worker.claim_job", return_value={"id": "job", "lease_token": "lease", "stage": "TRANSCRIBE"})
    @patch("app.worker.connect")
    def test_registered_stage_sets_its_persisted_state(self, connect, claim, finish):
        processor = lambda _job: "TRANSCRIBING"
        self.assertTrue(run_once("worker-a", {"TRANSCRIBE": processor}))
        self.assertEqual(finish.call_args.args[1:], ("job", "lease", {"processing_state": "TRANSCRIBING"}))
        self.assertEqual(claim.call_args.args[1:], ("worker-a", 60, ("TRANSCRIBE",)))

    @patch("app.worker.retry_job", return_value=True)
    @patch("app.worker.claim_job", return_value={"id": "job", "lease_token": "lease", "stage": "TRANSCRIBE"})
    @patch("app.worker.connect")
    def test_processor_failure_uses_bounded_retry_path(self, connect, claim, retry):
        def fail(_job):
            raise RuntimeError("raw transcript: customer secret-value")

        with self.assertLogs("app.worker", level=logging.INFO) as logs:
            self.assertTrue(run_once("worker-a", {"TRANSCRIBE": fail}))
        retry.assert_called_once_with(connect.return_value.__enter__.return_value, "job", "lease")
        self.assertNotIn("secret-value", "\n".join(logs.output))
        event = logs.records[0]
        self.assertEqual(event.event_name, "worker.stage_outcome")
        self.assertEqual(event.stage, "TRANSCRIBE")
        self.assertEqual(event.attempt, 0)
        self.assertEqual(event.outcome, "RETRY_HANDLED")
        self.assertFalse(hasattr(event, "call_id"))

    @patch("app.worker.finish_job", return_value=True)
    @patch("app.worker.claim_job", return_value={"id": "private-job", "call_id": str(uuid.uuid4()), "lease_token": "lease", "stage": "TRANSCRIBE", "attempts": 2})
    @patch("app.worker.connect")
    def test_worker_logs_stage_outcome_without_job_or_call_identifiers(self, connect, claim, finish):
        with self.assertLogs("app.worker", level=logging.INFO) as logs:
            self.assertTrue(run_once("worker-a", {"TRANSCRIBE": lambda _job: "TRANSCRIBING"}))
        self.assertEqual(len(logs.records), 1)
        event = logs.records[0]
        self.assertEqual((event.stage, event.attempt, event.outcome), ("TRANSCRIBE", 2, "COMMITTED"))
        self.assertGreaterEqual(event.duration_ms, 0)
        self.assertNotIn("private-job", "\n".join(logs.output))
        self.assertFalse(hasattr(event, "job_id"))
        self.assertFalse(hasattr(event, "call_id"))


if __name__ == "__main__":
    unittest.main()
