"""Safe operational aggregates expose no call-level identifiers."""

import unittest
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from unittest.mock import MagicMock
from uuid import uuid4

from app.operations import operations_summary


class OperationsSummaryTests(unittest.TestCase):
    def test_summary_is_tenant_scoped_and_clamps_queue_age(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (
            4, 50_000_000, 3,
            {"TRANSCRIBE": {"pending": 4, "oldest_pending_age_seconds": 50_000_000}, "SOMETHING_NEW": {"pending": 1, "oldest_pending_age_seconds": 5}},
        )

        result = operations_summary(connection, "org-a")

        self.assertEqual(
            result,
            {
                "pending_jobs": 4,
                "oldest_pending_age_seconds": 31_536_000,
                "incomplete_calls": 3,
                "pending_by_stage": {
                    "TRANSCRIBE": {"pending": 4, "oldest_pending_age_seconds": 31_536_000},
                    "UNKNOWN": {"pending": 1, "oldest_pending_age_seconds": 5},
                },
            },
        )
        self.assertEqual(connection.execute.call_args.args[1], ("org-a", 31_536_000, 31_536_000))
        self.assertEqual(set(result), {"pending_jobs", "oldest_pending_age_seconds", "incomplete_calls", "pending_by_stage"})


@unittest.skipUnless(os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class OperationsSummaryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database = (urlparse(os.environ.get("DATABASE_URL", "")).path or "").lstrip("/")
        if not database.endswith("_test"):
            raise RuntimeError("Refusing operations integration tests without isolated DATABASE_URL ending in _test")
        from app.migrate import migrate

        migrate()

    @staticmethod
    def _delete_jobs(*organisation_ids):
        from app.db import connect

        # Queued synthetic jobs would otherwise be claimed by later worker/ingest integration tests.
        with connect() as connection:
            connection.execute("DELETE FROM jobs WHERE organisation_id=ANY(%s)", (list(organisation_ids),))

    def test_summary_counts_only_visible_tenant_work_and_bounds_old_age(self):
        from app.db import connect

        organisation_id, other_organisation_id = uuid4(), uuid4()
        queued_call, review_call, tombstoned_call, other_call = (uuid4() for _ in range(4))
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s),(%s)", (organisation_id, other_organisation_id))
            for organisation, call_id, state, tombstoned_at in (
                (organisation_id, queued_call, "QUEUED", None),
                (organisation_id, review_call, "NEEDS_REVIEW", None),
                (organisation_id, tombstoned_call, "DELETING", datetime.now(timezone.utc)),
                (other_organisation_id, other_call, "QUEUED", None),
            ):
                connection.execute(
                    "INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state,tombstoned_at) "
                    "VALUES (%s,%s,%s,%s,%s,'agent','team','en',%s,%s)",
                    (organisation, call_id, str(call_id), str(call_id), "a" * 64, state, tombstoned_at),
                )
            connection.execute(
                "INSERT INTO jobs(organisation_id,id,call_id,stage,state,created_at) VALUES (%s,%s,%s,'TRANSCRIBE','QUEUED',%s),(%s,%s,%s,'TRANSCRIBE','QUEUED',now())",
                (organisation_id, uuid4(), queued_call, datetime.now(timezone.utc) - timedelta(days=500), other_organisation_id, uuid4(), other_call),
            )
        self.addCleanup(self._delete_jobs, organisation_id, other_organisation_id)
        with connect() as connection:
            result = operations_summary(connection, str(organisation_id))
        self.assertEqual(result["pending_jobs"], 1)
        self.assertEqual(result["oldest_pending_age_seconds"], 31_536_000)
        self.assertEqual(result["incomplete_calls"], 2)
        self.assertEqual(result["pending_by_stage"], {"TRANSCRIBE": {"pending": 1, "oldest_pending_age_seconds": 31_536_000}})


if __name__ == "__main__":
    unittest.main()
