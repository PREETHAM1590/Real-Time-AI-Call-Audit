import unittest
from unittest.mock import patch

from app.worker import run_once


class WorkerTests(unittest.TestCase):
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
            raise RuntimeError("synthetic failure")

        self.assertTrue(run_once("worker-a", {"TRANSCRIBE": fail}))
        retry.assert_called_once_with(connect.return_value.__enter__.return_value, "job", "lease")


if __name__ == "__main__":
    unittest.main()
