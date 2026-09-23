import unittest
from unittest.mock import patch

from app.worker import run_once


class WorkerTests(unittest.TestCase):
    @patch("app.worker.finish_job", return_value=True)
    @patch("app.worker.claim_job", return_value={"id": "job", "lease_token": "lease", "stage": "TRANSCRIBE"})
    @patch("app.worker.connect")
    def test_unconfigured_stage_finishes_as_needs_review(self, connect, claim, finish):
        connection = connect.return_value.__enter__.return_value
        self.assertTrue(run_once("worker-a"))
        claim.assert_called_once_with(connection, "worker-a")
        finish.assert_called_once_with(connection, "job", "lease", {"processing_state": "NEEDS_REVIEW"})

    @patch("app.worker.finish_job", return_value=True)
    @patch("app.worker.claim_job", return_value={"id": "job", "lease_token": "lease", "stage": "TRANSCRIBE"})
    @patch("app.worker.connect")
    def test_registered_stage_sets_its_persisted_state(self, connect, claim, finish):
        processor = lambda _job: "TRANSCRIBING"
        self.assertTrue(run_once("worker-a", {"TRANSCRIBE": processor}))
        finish.assert_called_once_with(connect.return_value.__enter__.return_value, "job", "lease", {"processing_state": "TRANSCRIBING"})


if __name__ == "__main__":
    unittest.main()
