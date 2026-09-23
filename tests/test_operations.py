"""Synthetic deletion lifecycle and deterministic quality evaluation checks."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from app.db import connect
from app.migrate import migrate
from app.retention import RetentionError, due_for_deletion, purge_call, request_call_deletion, tombstone_expired_calls
from app.evaluate import evaluate_dataset, evaluate_records, precision_recall
from app.storage import LocalPrivateStorage


class RetentionBoundaryTests(unittest.TestCase):
    def test_expiry_boundary_and_hold(self):
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        self.assertTrue(due_for_deletion(now, now, False))
        self.assertFalse(due_for_deletion(now, now, True))
        self.assertFalse(due_for_deletion(now + timedelta(seconds=1), now, False))

    def test_expiry_requires_aware_utc_timestamps(self):
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            due_for_deletion(datetime(2026, 9, 23), now, False)
        with self.assertRaises(ValueError):
            due_for_deletion(now, datetime(2026, 9, 23), False)
        with self.assertRaises(ValueError):
            due_for_deletion(now, now, 1)

    def test_expiry_sweep_rejects_unbounded_batches(self):
        with self.assertRaises(RetentionError):
            tombstone_expired_calls(None, limit=101)
        with self.assertRaises(RetentionError):
            tombstone_expired_calls(None, limit=0)


class EvaluationTests(unittest.TestCase):
    def test_undefined_ratios_are_null_not_perfect(self):
        self.assertEqual(precision_recall(0, 0, 0), {"precision": None, "recall": None})
        self.assertEqual(precision_recall(0, 1, 0), {"precision": 0.0, "recall": None})

    def test_synthetic_report_counts_abstention_exclusion_zero_positive_and_critical_miss(self):
        records = [
            {"case_id": "case-positive", "excluded": False, "abstained": False, "gold_findings": [{"rule_id": "disclosure", "critical": True}], "predicted_findings": [{"rule_id": "disclosure", "status": "POTENTIAL_VIOLATION"}], "gold_dimensions": {"clarity": 4}, "predicted_dimensions": {"clarity": 3}},
            {"case_id": "case-missed", "excluded": False, "abstained": False, "gold_findings": [{"rule_id": "critical_close", "critical": True}], "predicted_findings": [], "gold_dimensions": {"clarity": 3, "greeting": 4}, "predicted_dimensions": {"clarity": 3}},
            {"case_id": "case-zero-positive", "excluded": False, "abstained": False, "gold_findings": [], "predicted_findings": [], "gold_dimensions": {}, "predicted_dimensions": {}},
            {"case_id": "case-abstained", "excluded": False, "abstained": True, "gold_findings": [], "predicted_findings": [], "gold_dimensions": {"clarity": 5}, "predicted_dimensions": {}},
            {"case_id": "case-excluded", "excluded": True, "abstained": False, "gold_findings": [{"rule_id": "not-counted", "critical": False}], "predicted_findings": [], "gold_dimensions": {}, "predicted_dimensions": {}},
        ]
        report = evaluate_records(records)
        self.assertEqual(report["sample_counts"], {"total": 5, "explicitly_excluded": 1, "eligible": 4, "abstained": 1, "covered": 3})
        self.assertEqual(report["coverage"], 0.75)
        self.assertEqual(report["findings_by_rule"]["critical_close"]["recall"], 0.0)
        self.assertNotIn("not-counted", report["findings_by_rule"])
        self.assertEqual(report["missed_critical_findings"], 1)
        self.assertEqual(report["score_agreement"]["adjudicated_dimensions"], 2)
        self.assertEqual(report["score_agreement"]["applicable_dimensions"], 4)
        self.assertEqual(report["score_agreement"]["excluded_dimensions"], 2)
        self.assertAlmostEqual(report["score_agreement"]["mean_absolute_error"], 0.5)

    def test_evaluation_cli_is_deterministic_and_rejects_unknown_fields(self):
        record = {"case_id": "case-zero-positive", "excluded": False, "abstained": False, "gold_findings": [], "predicted_findings": [], "gold_dimensions": {}, "predicted_dimensions": {}}
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "cases.jsonl"
            first, second = Path(directory) / "first.json", Path(directory) / "second.json"
            dataset.write_text(json.dumps(record) + "\n", encoding="utf-8")
            command = [sys.executable, "-m", "app.evaluate", "--dataset", str(dataset), "--output"]
            one = subprocess.run(command + [str(first)], capture_output=True, text=True, check=False)
            two = subprocess.run(command + [str(second)], capture_output=True, text=True, check=False)
            self.assertEqual(one.returncode, 0, one.stderr)
            self.assertEqual(two.returncode, 0, two.stderr)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            dataset.write_text(json.dumps({**record, "transcript": "never accept raw text"}) + "\n", encoding="utf-8")
            invalid = subprocess.run(command + [str(second)], capture_output=True, text=True, check=False)
            self.assertEqual(invalid.returncode, 2)

    def test_checked_in_synthetic_golden_fixture_records_critical_miss_and_abstention(self):
        fixture = Path(__file__).parent / "fixtures" / "golden.jsonl"
        report = evaluate_dataset(fixture)
        self.assertEqual(report["sample_counts"]["explicitly_excluded"], 1)
        self.assertEqual(report["sample_counts"]["abstained"], 1)
        self.assertEqual(report["missed_critical_findings"], 1)
        self.assertNotIn("excluded_finding", report["findings_by_rule"])


@unittest.skipUnless(os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class RetentionPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database = (urlparse(os.environ.get("DATABASE_URL", "")).path or "").lstrip("/")
        if not database.endswith("_test"):
            raise RuntimeError("Refusing retention integration tests without isolated DATABASE_URL ending in _test")
        migrate()

    def make_call(self, *, held=False, expires_at=None):
        organisation_id, call_id, audio_id = uuid4(), uuid4(), uuid4()
        storage = LocalPrivateStorage(Path(tempfile.gettempdir()) / f"call-audit-retention-{uuid4().hex}")
        storage_key = storage.put(b"synthetic audio bytes")
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s)", (organisation_id,))
            connection.execute(
                "INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state,transcript_revision,retention_expires_at,legal_hold) "
                "VALUES (%s,%s,%s,%s,%s,'agent-private','team-private','en','QUEUED',1,%s,%s)",
                (organisation_id, call_id, str(call_id), str(call_id), "a" * 64, expires_at, held),
            )
            connection.execute(
                "INSERT INTO audio_objects(organisation_id,id,call_id,private_key,checksum,codec,sample_rate,channels,duration_ms) "
                "VALUES (%s,%s,%s,%s,%s,'wav',16000,1,1000)",
                (organisation_id, audio_id, call_id, storage_key, "a" * 64),
            )
            job_id = uuid4()
            connection.execute("INSERT INTO jobs(organisation_id,id,call_id,stage,state) VALUES (%s,%s,%s,'TRANSCRIBE','QUEUED')", (organisation_id, job_id, call_id))
            connection.execute(
                "INSERT INTO transcript_utterances(organisation_id,call_id,revision,id,segment_id,speaker_id,role,start_ms,end_ms,text_redacted,confidence,model_version) "
                "VALUES (%s,%s,1,'u1','s1','speaker-1','AGENT',0,100,'Synthetic redacted text.',0.9,'test-v1')",
                (organisation_id, call_id),
            )
            audit_id = uuid4()
            dimensions = [{"id": "greeting", "status": "SCORED", "score": 3, "reason": "Synthetic redacted evidence", "evidence": []}]
            scores = {"greeting": 3}
            connection.execute(
                "INSERT INTO audits(organisation_id,id,call_id,revision,transcript_revision,model_artifact,inference_runtime,prompt_version,prompt_hash,rubric_version,rubric_hash,policy_provenance,policy_fingerprint,dimensions_json,overall_score,decision,coaching_narrative,highlights,improvement_areas,usage_json,attempts,latency_ms,pass_threshold) "
                "VALUES (%s,%s,%s,1,1,'synthetic','synthetic','prompt-v1',%s,'rubric-v1',%s,'[]'::jsonb,%s,%s::jsonb,3,'PASS','{}'::jsonb,'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,1,1,3)",
                (organisation_id, audit_id, call_id, "b" * 64, "c" * 64, "d" * 64, json.dumps(dimensions)),
            )
            connection.execute(
                "INSERT INTO reviews(organisation_id,id,audit_id,call_id,audit_revision,version,base_review_version,reviewer_id,action,changed_scores_json,effective_scores_json,effective_score,effective_decision,reason) "
                "VALUES (%s,%s,%s,%s,1,1,0,'reviewer','ACCEPT','{}'::jsonb,%s::jsonb,3,'PASS','Synthetic restricted review reason')",
                (organisation_id, uuid4(), audit_id, call_id, json.dumps(scores)),
            )
        return organisation_id, call_id, job_id, storage, storage_key

    def test_tombstone_precedes_bounded_purge_and_is_idempotent(self):
        organisation_id, call_id, job_id, storage, storage_key = self.make_call()
        with connect() as connection:
            requested = request_call_deletion(connection, str(organisation_id), str(call_id), "admin-user", now=datetime(2026, 9, 23, tzinfo=timezone.utc))
            self.assertEqual(requested["status"], "TOMBSTONED")
            row = connection.execute("SELECT tombstoned_at,processing_state FROM calls WHERE organisation_id=%s AND id=%s", (organisation_id, call_id)).fetchone()
            job = connection.execute("SELECT state,last_error_code FROM jobs WHERE organisation_id=%s AND id=%s", (organisation_id, job_id)).fetchone()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1], "DELETING")
            self.assertEqual(job, ("FAILED", "TOMBSTONED"))
            result = purge_call(connection, str(organisation_id), str(call_id), storage, now=datetime(2026, 9, 23, tzinfo=timezone.utc))
            repeated = purge_call(connection, str(organisation_id), str(call_id), storage, now=datetime(2026, 9, 23, tzinfo=timezone.utc))
            self.assertEqual(result["status"], "PARTIAL_IMMUTABLE_HISTORY")
            self.assertEqual(repeated["status"], "ALREADY_PURGED")
            call = connection.execute("SELECT processing_state,agent_id,team_id,content_purged_at FROM calls WHERE organisation_id=%s AND id=%s", (organisation_id, call_id)).fetchone()
            self.assertEqual(call, ("DELETED", "[deleted]", "[deleted]", call[3]))
            self.assertIsNotNone(call[3])
            self.assertIsNone(connection.execute("SELECT 1 FROM audio_objects WHERE organisation_id=%s AND call_id=%s", (organisation_id, call_id)).fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM transcript_utterances WHERE organisation_id=%s AND call_id=%s", (organisation_id, call_id)).fetchone())
            self.assertFalse(Path(storage.root / storage_key).exists())
            self.assertEqual(result["immutable_records_retained"]["audits"], 1)
            self.assertEqual(result["immutable_records_retained"]["reviews"], 1)
            self.assertIsNotNone(connection.execute("SELECT 1 FROM audits WHERE organisation_id=%s AND call_id=%s", (organisation_id, call_id)).fetchone())
            self.assertIsNotNone(connection.execute("SELECT 1 FROM reviews WHERE organisation_id=%s AND call_id=%s", (organisation_id, call_id)).fetchone())
            events = connection.execute(
                "SELECT action,details FROM access_events WHERE organisation_id=%s AND resource_type='CALL' AND resource_id=%s",
                (organisation_id, call_id),
            ).fetchall()
            event_details = {row[0]: row[1] for row in events}
            self.assertEqual(set(event_details), {"CALL_DELETION_REQUESTED", "CALL_CONTENT_PURGED"})
            self.assertEqual(event_details["CALL_CONTENT_PURGED"], {"retained_immutable_history": {"audits": 1, "reviews": 1, "dispositions": 0}})

    def test_hold_and_not_due_calls_are_not_physically_purged(self):
        organisation_id, call_id, _, storage, storage_key = self.make_call(held=True, expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        with connect() as connection:
            held = request_call_deletion(connection, str(organisation_id), str(call_id), "admin-user", now=datetime(2026, 9, 23, tzinfo=timezone.utc))
            self.assertEqual(held["status"], "HELD")
            self.assertTrue(Path(storage.root / storage_key).exists())
            expired = tombstone_expired_calls(connection, now=datetime(2026, 9, 23, tzinfo=timezone.utc), limit=10, organisation_id=str(organisation_id))
            self.assertEqual(expired, 0)
            blocked = purge_call(connection, str(organisation_id), str(call_id), storage, now=datetime(2026, 9, 23, tzinfo=timezone.utc))
            self.assertEqual(blocked["status"], "HELD")

        other_org, not_due_call, _, not_due_storage, not_due_key = self.make_call(expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
        with connect() as connection:
            not_due = purge_call(connection, str(other_org), str(not_due_call), not_due_storage, now=datetime(2026, 9, 23, tzinfo=timezone.utc))
        self.assertEqual(not_due["status"], "NOT_TOMBSTONED")
        self.assertTrue(Path(not_due_storage.root / not_due_key).exists())

    def test_expiry_sweep_is_bounded_tombstones_before_purge(self):
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        organisation_id, call_id, _, storage, storage_key = self.make_call(expires_at=now - timedelta(seconds=1))
        with connect() as connection:
            self.assertEqual(tombstone_expired_calls(connection, now=now, limit=1, organisation_id=str(organisation_id)), 1)
            row = connection.execute("SELECT tombstoned_at,processing_state FROM calls WHERE organisation_id=%s AND id=%s", (organisation_id, call_id)).fetchone()
            self.assertIsNotNone(row[0])
            self.assertEqual(row[1], "DELETING")
            result = purge_call(connection, str(organisation_id), str(call_id), storage, now=now)
        self.assertEqual(result["status"], "PARTIAL_IMMUTABLE_HISTORY")
        self.assertFalse(Path(storage.root / storage_key).exists())

    def test_purge_rejects_more_than_the_per_call_audio_bound_before_deleting(self):
        organisation_id, call_id, _, storage, first_key = self.make_call()
        extra_keys = [storage.put(b"synthetic extra audio") for _ in range(16)]
        with connect() as connection:
            for storage_key in extra_keys:
                connection.execute(
                    "INSERT INTO audio_objects(organisation_id,id,call_id,private_key,checksum,codec,sample_rate,channels,duration_ms) "
                    "VALUES (%s,%s,%s,%s,%s,'wav',16000,1,1000)",
                    (organisation_id, uuid4(), call_id, storage_key, "a" * 64),
                )
            request_call_deletion(connection, str(organisation_id), str(call_id), "admin-user")
            with self.assertRaises(RetentionError):
                purge_call(connection, str(organisation_id), str(call_id), storage)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM audio_objects WHERE organisation_id=%s AND call_id=%s", (organisation_id, call_id)).fetchone()[0],
                17,
            )
        self.assertTrue(all(Path(storage.root / key).exists() for key in [first_key, *extra_keys]))

    def test_running_worker_cannot_commit_after_deletion_tombstone(self):
        from app.ingest import finish_job

        organisation_id, call_id, job_id, _, _ = self.make_call()
        with connect() as connection:
            lease_token = uuid4()
            lease_until = datetime.now(timezone.utc) + timedelta(minutes=2)
            connection.execute(
                "UPDATE jobs SET state='RUNNING',lease_token=%s,lease_until=%s WHERE organisation_id=%s AND id=%s",
                (lease_token, lease_until, organisation_id, job_id),
            )
            requested = request_call_deletion(connection, str(organisation_id), str(call_id), "admin-user")
            self.assertEqual(requested["status"], "TOMBSTONED")
            committed = finish_job(
                connection,
                str(job_id),
                str(lease_token),
                {"utterances": [{"id": "late", "segment_id": "late", "speaker_id": "ch-0", "role": "AGENT", "start_ms": 200, "end_ms": 300, "text_redacted": "late result", "confidence": 0.9}], "model_version": "test-v1"},
            )
            self.assertFalse(committed)
            self.assertIsNone(connection.execute("SELECT 1 FROM transcript_utterances WHERE organisation_id=%s AND call_id=%s AND revision=2", (organisation_id, call_id)).fetchone())


if __name__ == "__main__":
    unittest.main()
