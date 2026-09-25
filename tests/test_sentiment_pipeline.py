"""Wiring of the local sentiment adapter into the post-call pipeline.

Unit-level: `make_sentiment_processor` (mocked connection) and `build_processors`
(env-var gating, missing `transformers`, registration). Integration-level (requires
`RUN_POSTGRES_INTEGRATION=1` and an isolated `_test` database): enqueue-after-commit,
persistence, idempotency, staleness, text-leak rejection, immutability-but-deletable,
retention purge, and tenant-scoped `call_detail` exposure.

`app.sentiment_adapter.py` and `app.sentiment.py` already have their own unit tests
(`tests/test_sentiment_adapter.py`); this file covers only the new pipeline wiring.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase, main, skipUnless
from unittest.mock import Mock, patch
from urllib.parse import urlparse
from uuid import uuid4

from app.artifacts import directory_sha256
from app.auth import Scope
from app.db import connect
from app.ingest import finish_job, persist_call_sentiment
from app.migrate import migrate
from app.reviews import ReviewNotFound, call_detail
from app.retention import purge_call, request_call_deletion
from app.sentiment_adapter import LocalSentimentAdapter
from app.storage import LocalPrivateStorage
from app.worker import build_processors, make_sentiment_processor


def _verified_model_directory(tmp_path) -> tuple[str, str]:
    root = Path(tmp_path)
    (root / "config.json").write_text("{}")
    return str(root), directory_sha256(root)


class MakeSentimentProcessorTests(TestCase):
    @patch("app.worker.connect")
    def test_only_final_customer_text_reaches_classifier_and_output_carries_no_text(self, connect):
        connection = connect.return_value.__enter__.return_value
        rows = [
            ("u1", "AGENT", 0, 1000, "agent line", True),
            ("u2", "CUSTOMER", 1000, 2000, "customer partial line", False),
            ("u3", "UNKNOWN", 2000, 3000, "mono line", True),
            ("u4", "IVR", 3000, 4000, "ivr line", True),
            ("u5", "CUSTOMER", 4000, 5000, "final customer line", True),
        ]
        connection.execute.side_effect = [
            Mock(fetchone=Mock(return_value=(1,))),
            Mock(fetchall=Mock(return_value=rows)),
        ]
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)
            adapter = LocalSentimentAdapter(
                artifact_path=path, artifact_sha256=digest,
                classify=lambda text: sent.append(text) or [{"label": "neutral", "score": 1.0}],
            )
            job = {"organisation_id": str(uuid4()), "call_id": str(uuid4()), "input_revision": 1}
            output = make_sentiment_processor(adapter)(job)
        self.assertEqual(sent, ["final customer line"])
        dumped = json.dumps(output)
        for banned in ("text_redacted", "agent line", "customer partial line", "mono line", "ivr line", "final customer line"):
            self.assertNotIn(banned, dumped)
        self.assertEqual(output["sentiment"]["status"], "OK")
        self.assertEqual([signal["id"] for signal in output["sentiment"]["signals"]], ["u5"])
        self.assertEqual(set(output["sentiment"]["signals"][0]), {"id", "start_ms", "end_ms", "signed_score", "top_class_probability"})
        self.assertEqual(set(output["sentiment"]), {"transcript_revision", "model_artifact", "adapter_version", "status", "signals", "alert_offsets_ms", "trend", "failed_utterance_count"})

    @patch("app.worker.connect")
    def test_stale_transcript_revision_raises_before_calling_the_adapter(self, connect):
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = (2,)
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)
            adapter = LocalSentimentAdapter(artifact_path=path, artifact_sha256=digest, classify=lambda text: calls.append(text) or [{"label": "neutral", "score": 1.0}])
            job = {"organisation_id": str(uuid4()), "call_id": str(uuid4()), "input_revision": 1}
            with self.assertRaises(RuntimeError):
                make_sentiment_processor(adapter)(job)
        self.assertEqual(calls, [])


class BuildProcessorsSentimentTests(TestCase):
    def test_raises_when_only_one_env_var_is_set(self):
        with patch.dict(os.environ, {"SENTIMENT_MODEL_PATH": "/nonexistent"}, clear=True):
            with self.assertRaises(RuntimeError):
                build_processors()
        with patch.dict(os.environ, {"SENTIMENT_MODEL_SHA256": "a" * 64}, clear=True):
            with self.assertRaises(RuntimeError):
                build_processors()

    def test_raises_a_clear_error_when_transformers_is_not_importable(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)
            with patch.dict(os.environ, {"SENTIMENT_MODEL_PATH": path, "SENTIMENT_MODEL_SHA256": digest}, clear=True), \
                 patch.dict(sys.modules, {"transformers": None}):
                with self.assertRaises(RuntimeError) as context:
                    build_processors()
        self.assertIn("transformers", str(context.exception))

    def test_registers_sentiment_stage_when_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            path, digest = _verified_model_directory(directory)
            fake_classify = lambda text: [{"label": "neutral", "score": 1.0}]
            with patch.dict(os.environ, {"SENTIMENT_MODEL_PATH": path, "SENTIMENT_MODEL_SHA256": digest}, clear=True), \
                 patch("app.sentiment_adapter.transformers_classifier", return_value=fake_classify) as classifier_patch:
                processors = build_processors()
        self.assertIn("SENTIMENT", processors)
        classifier_patch.assert_called_once()


@skipUnless(os.environ.get("DATABASE_URL") or os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class SentimentPipelineIntegrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        database = (urlparse(os.environ.get("DATABASE_URL", "")).path or "").lstrip("/")
        if not database.endswith("_test"):
            raise RuntimeError("Refusing sentiment integration tests without isolated DATABASE_URL ending in _test")
        migrate()

    def setUp(self):
        self.org, self.other_org, self.call = uuid4(), uuid4(), uuid4()
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s),(%s)", (self.org, self.other_org))
            connection.execute(
                "INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state) "
                "VALUES (%s,%s,%s,%s,%s,'agent-a','team-a','en','TRANSCRIBING')",
                (self.org, self.call, f"sentiment-{self.call}", f"sentiment-{self.call}", "a" * 64),
            )
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        # jobs must not leak QUEUED/RUNNING rows into unrelated tests' worker polling
        # (see AGENTS.md). access_events/organisations are immutable-by-design (some
        # tests here call request_call_deletion/purge_call, which write access_events
        # rows an FK ties to organisations) and safely left behind, matching the same
        # never-delete-organisations precedent in tests/test_operations.py.
        with connect() as connection:
            for organisation_id in (self.org, self.other_org):
                connection.execute("DELETE FROM call_sentiments WHERE organisation_id=%s", (organisation_id,))
                connection.execute("DELETE FROM jobs WHERE organisation_id=%s", (organisation_id,))
                connection.execute("DELETE FROM transcript_utterances WHERE organisation_id=%s", (organisation_id,))
                connection.execute("DELETE FROM findings WHERE organisation_id=%s", (organisation_id,))
                connection.execute("DELETE FROM audio_objects WHERE organisation_id=%s", (organisation_id,))
                connection.execute("DELETE FROM events WHERE organisation_id=%s", (organisation_id,))
                connection.execute("DELETE FROM calls WHERE organisation_id=%s", (organisation_id,))
                connection.execute("DELETE FROM event_counters WHERE organisation_id=%s", (organisation_id,))

    def _at_revision(self, revision: int = 1, *, processing_state: str = "ANALYSING") -> None:
        with connect() as connection:
            connection.execute(
                "UPDATE calls SET transcript_revision=%s,processing_state=%s WHERE organisation_id=%s AND id=%s",
                (revision, processing_state, self.org, self.call),
            )
            connection.execute(
                "INSERT INTO transcript_utterances(organisation_id,call_id,revision,id,segment_id,speaker_id,role,start_ms,end_ms,text_redacted,confidence,model_version,is_final) "
                "VALUES (%s,%s,%s,%s,%s,'channel-0','CUSTOMER',0,1000,'Synthetic redacted customer line.',0.9,'fake-asr',true)",
                (self.org, self.call, revision, f"utt-r{revision}", f"seg-r{revision}"),
            )

    def _running_sentiment_job(self, revision: int = 1):
        job_id, lease_token = uuid4(), uuid4()
        with connect() as connection:
            connection.execute(
                "INSERT INTO jobs(organisation_id,id,call_id,stage,input_revision,state,attempts,lease_token,lease_until) "
                "VALUES (%s,%s,%s,'SENTIMENT',%s,'RUNNING',1,%s,now()+interval '5 minutes')",
                (self.org, job_id, self.call, revision, lease_token),
            )
        return job_id, lease_token

    @staticmethod
    def _sample_sentiment(transcript_revision: int = 1, *, status: str = "OK", signal_id: str = "utt-r1") -> dict:
        if status != "OK":
            return {
                "transcript_revision": transcript_revision, "model_artifact": "sha256:" + "a" * 64,
                "adapter_version": "local-sentiment-v1", "status": status, "signals": [],
                "alert_offsets_ms": [], "trend": None, "failed_utterance_count": 0,
            }
        return {
            "transcript_revision": transcript_revision, "model_artifact": "sha256:" + "a" * 64,
            "adapter_version": "local-sentiment-v1", "status": "OK",
            "signals": [{"id": signal_id, "start_ms": 0, "end_ms": 1000, "signed_score": 0.5, "top_class_probability": 0.9}],
            "alert_offsets_ms": [60_000],
            "trend": {"first_60s_mean_signed_score": 0.5, "first_60s_customer_speech_ms": 1000, "last_60s_mean_signed_score": 0.5, "last_60s_customer_speech_ms": 1000, "trend_delta": 0.0},
            "failed_utterance_count": 0,
        }

    def test_sentiment_job_is_enqueued_alongside_analyse_and_policy_after_transcript_commit(self):
        job_id, lease_token = uuid4(), uuid4()
        with connect() as connection:
            connection.execute(
                "INSERT INTO jobs(organisation_id,id,call_id,stage,state,attempts,lease_token,lease_until) "
                "VALUES (%s,%s,%s,'TRANSCRIBE','RUNNING',1,%s,now()+interval '1 minute')",
                (self.org, job_id, self.call, lease_token),
            )
            row = {"id": "utterance-1", "segment_id": "segment-1", "speaker_id": "channel-0", "role": "CUSTOMER", "start_ms": 0, "end_ms": 500, "text_redacted": "Hello there.", "confidence": None, "is_final": True}
            self.assertTrue(finish_job(connection, str(job_id), str(lease_token), {
                "processing_state": "ANALYSING", "model_version": "local@sha256", "utterances": [row],
            }))
            rows = connection.execute("SELECT stage,state,input_revision FROM jobs WHERE organisation_id=%s AND call_id=%s AND stage IN ('ANALYSE','POLICY','SENTIMENT')", (self.org, self.call)).fetchall()
            jobs = {row[0]: (row[1], row[2]) for row in rows}
        self.assertEqual(jobs["SENTIMENT"], ("QUEUED", 1))
        self.assertEqual(jobs["ANALYSE"], ("QUEUED", 1))
        self.assertEqual(jobs["POLICY"], ("QUEUED", 1))

    def test_finish_job_persists_sentiment_and_leaves_processing_state_unchanged(self):
        self._at_revision(1, processing_state="NEEDS_REVIEW")
        job_id, lease_token = self._running_sentiment_job(1)
        with connect() as connection:
            self.assertTrue(finish_job(connection, str(job_id), str(lease_token), {"sentiment": self._sample_sentiment(1)}))
            row = connection.execute(
                "SELECT revision,transcript_revision,status,model_artifact,adapter_version,failed_utterance_count,jsonb_array_length(signals_json) "
                "FROM call_sentiments WHERE organisation_id=%s AND call_id=%s", (self.org, self.call),
            ).fetchone()
            self.assertEqual(row, (1, 1, "OK", "sha256:" + "a" * 64, "local-sentiment-v1", 0, 1))
            self.assertEqual(connection.execute("SELECT processing_state FROM calls WHERE organisation_id=%s AND id=%s", (self.org, self.call)).fetchone()[0], "NEEDS_REVIEW")

    def test_persisting_the_same_sentiment_provenance_twice_is_idempotent(self):
        # The jobs table's own (organisation_id,call_id,stage,input_revision) unique
        # constraint already prevents two SENTIMENT job rows for one revision, so the
        # realistic rerun this covers is a job re-attempt calling persist_call_sentiment
        # again for the same (transcript_revision, model_artifact, adapter_version).
        self._at_revision(1)
        sentiment = self._sample_sentiment(1)
        with connect() as connection:
            persist_call_sentiment(connection, self.org, self.call, 1, sentiment)
            persist_call_sentiment(connection, self.org, self.call, 1, sentiment)
            connection.commit()
            count = connection.execute("SELECT count(*) FROM call_sentiments WHERE organisation_id=%s AND call_id=%s", (self.org, self.call)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_rejects_a_stale_transcript_revision(self):
        self._at_revision(1)
        job_id, lease_token = self._running_sentiment_job(1)
        with connect() as connection:
            connection.execute("UPDATE calls SET transcript_revision=2 WHERE organisation_id=%s AND id=%s", (self.org, self.call))
            with self.assertRaises(ValueError):
                finish_job(connection, str(job_id), str(lease_token), {"sentiment": self._sample_sentiment(1)})

    def test_rejects_a_signal_carrying_a_text_key(self):
        self._at_revision(1)
        job_id, lease_token = self._running_sentiment_job(1)
        sentiment = self._sample_sentiment(1)
        sentiment["signals"][0]["text"] = "should never be persisted"
        with connect() as connection:
            with self.assertRaises(ValueError):
                finish_job(connection, str(job_id), str(lease_token), {"sentiment": sentiment})
            count = connection.execute("SELECT count(*) FROM call_sentiments WHERE organisation_id=%s AND call_id=%s", (self.org, self.call)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_update_is_rejected_but_delete_remains_allowed(self):
        self._at_revision(1)
        job_id, lease_token = self._running_sentiment_job(1)
        with connect() as connection:
            self.assertTrue(finish_job(connection, str(job_id), str(lease_token), {"sentiment": self._sample_sentiment(1)}))
        from psycopg.errors import RaiseException

        with connect() as connection:
            with self.assertRaises(RaiseException):
                connection.execute("UPDATE call_sentiments SET status='UNKNOWN' WHERE organisation_id=%s AND call_id=%s", (self.org, self.call))
        with connect() as connection:
            deleted = connection.execute("DELETE FROM call_sentiments WHERE organisation_id=%s AND call_id=%s", (self.org, self.call)).rowcount
        self.assertEqual(deleted, 1)

    def test_purge_call_removes_call_sentiments_rows(self):
        self._at_revision(1)
        job_id, lease_token = self._running_sentiment_job(1)
        with connect() as connection:
            self.assertTrue(finish_job(connection, str(job_id), str(lease_token), {"sentiment": self._sample_sentiment(1)}))
        storage = LocalPrivateStorage(Path(tempfile.gettempdir()) / f"call-audit-sentiment-{uuid4().hex}")
        now = datetime(2026, 9, 25, tzinfo=timezone.utc)
        with connect() as connection:
            requested = request_call_deletion(connection, str(self.org), str(self.call), "admin-user", now=now)
            self.assertEqual(requested["status"], "TOMBSTONED")
            result = purge_call(connection, str(self.org), str(self.call), storage, now=now)
            self.assertIn(result["status"], {"CONTENT_PURGED", "PARTIAL_IMMUTABLE_HISTORY"})
            remaining = connection.execute("SELECT count(*) FROM call_sentiments WHERE organisation_id=%s AND call_id=%s", (self.org, self.call)).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_call_detail_exposes_sentiment_summary_scoped_to_the_owning_organisation(self):
        self._at_revision(1)
        job_id, lease_token = self._running_sentiment_job(1)
        with connect() as connection:
            self.assertTrue(finish_job(connection, str(job_id), str(lease_token), {"sentiment": self._sample_sentiment(1)}))
            scope_a = Scope(str(self.org), "qa-a", "QA_ANALYST", frozenset())
            detail = call_detail(connection, scope_a, str(self.call))
            self.assertIsNotNone(detail["sentiment"])
            self.assertEqual(detail["sentiment"]["status"], "OK")
            self.assertEqual(detail["sentiment"]["signal_count"], 1)
            self.assertNotIn("signals", detail["sentiment"])
            self.assertNotIn("Synthetic redacted customer line.", json.dumps(detail["sentiment"]))
            scope_b = Scope(str(self.other_org), "qa-b", "QA_ANALYST", frozenset())
            with self.assertRaises(ReviewNotFound):
                call_detail(connection, scope_b, str(self.call))
            leak = connection.execute("SELECT count(*) FROM call_sentiments WHERE organisation_id=%s", (self.other_org,)).fetchone()[0]
        self.assertEqual(leak, 0)


if __name__ == "__main__":
    main()
