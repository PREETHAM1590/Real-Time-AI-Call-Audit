import hashlib
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import ValidationError
from psycopg import sql
from app.artifacts import directory_sha256, verified_model_directory
from app.db import connect
from app.ingest import finish_job
from app.migrate import migrate
from app.privacy import RedactionError, redact_text
from app.storage import LocalPrivateStorage
from app.transcription import (
    PreparedUtterance,
    TranscriptionError,
    make_transcription_processor,
    normalise_segments,
    prepare_utterances,
    transcribe_recording,
)


class StaticAnalyzer:
    def __init__(self, spans=(), fail=False):
        self.spans = spans
        self.fail = fail

    def analyze(self, *, text, language):
        if self.fail:
            raise RuntimeError(f"unsafe error containing {text}")
        return [SimpleNamespace(start=start, end=end, entity_type="PERSON") for start, end in self.spans]


class FakeStorage:
    def __init__(self, audio=b"synthetic audio"):
        self.audio = audio

    def get(self, key, *, max_bytes):
        if key != "call.audio":
            raise ValueError("wrong key")
        return self.audio


class FakeModel:
    def __init__(self, segments):
        self.segments = segments
        self.received = None
        self.options = None

    def transcribe(self, audio, **kwargs):
        self.received = audio.read()
        self.options = kwargs
        return iter(self.segments), SimpleNamespace(language="en")


class PrivacyTests(unittest.TestCase):
    def test_structured_pii_and_presidio_name_address_spans_are_redacted(self):
        text = "Email jane.doe@example.com or call +1 (415) 555-0199; name Jane Doe lives at 5 Oak Street."
        analyzer = StaticAnalyzer([(text.index("Jane Doe"), text.index("Jane Doe") + len("Jane Doe")), (text.index("5 Oak Street"), text.index("5 Oak Street") + len("5 Oak Street"))])
        redacted = redact_text(text, analyzer)
        for secret in ("jane.doe@example.com", "+1 (415) 555-0199", "Jane Doe", "5 Oak Street"):
            self.assertNotIn(secret, redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 4)

    def test_redaction_failure_is_closed_and_error_contains_no_transcript(self):
        secret = "Mira Example"
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        logger = logging.getLogger()
        logger.addHandler(handler)
        try:
            with self.assertRaises(RedactionError) as raised:
                redact_text(secret, StaticAnalyzer(fail=True))
        finally:
            logger.removeHandler(handler)
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, sink.getvalue())

    def test_unconfigured_language_fails_closed(self):
        with self.assertRaises(RedactionError):
            redact_text("texto", StaticAnalyzer(), language="es")

    def test_default_redactor_fails_closed_without_local_presidio_model(self):
        with patch.dict(os.environ, {"PRESIDIO_SPACY_MODEL_PATH": "", "PRESIDIO_SPACY_MODEL_SHA256": ""}):
            from app.privacy import _configured_analyzer
            _configured_analyzer.cache_clear()
            try:
                with self.assertRaises(RedactionError):
                    redact_text("ordinary text")
            finally:
                _configured_analyzer.cache_clear()


class TranscriptionTests(unittest.TestCase):
    def test_mono_speaker_is_unknown_silence_dropped_and_overlaps_retained(self):
        normalized = normalise_segments({"segments": [
            {"start": 0, "end": 0.1, "text": "   "},
            {"start": 0.2, "end": 1.2, "text": "first"},
            {"start": 0.8, "end": 1.5, "text": "overlap"},
        ]}, {})
        self.assertEqual([row["text"] for row in normalized], ["first", "overlap"])
        self.assertEqual([row["role"] for row in normalized], ["UNKNOWN", "UNKNOWN"])
        self.assertEqual(normalized[0]["speaker_id"], "mono-unknown")
        self.assertLess(normalized[1]["start_ms"], normalized[0]["end_ms"])

    def test_explicit_channel_mapping_is_used_only_for_known_channel(self):
        rows = normalise_segments({"segments": [
            {"start": 0, "end": 1, "text": "hello", "channel": 1},
            {"start": 1, "end": 2, "text": "there", "channel": 2},
        ]}, {1: "CUSTOMER"})
        self.assertEqual([row["role"] for row in rows], ["CUSTOMER", "UNKNOWN"])

    def test_prepare_batch_returns_only_redacted_final_items(self):
        source = "My phone is 4155550199"
        segments = normalise_segments({"segments": [{"start": 0, "end": 1, "text": source}]}, {})
        prepared = prepare_utterances(segments, lambda text: text.replace("4155550199", "[REDACTED]"))
        self.assertEqual(len(prepared), 1)
        self.assertIsInstance(prepared[0], PreparedUtterance)
        self.assertTrue(prepared[0].is_final)
        self.assertNotIn("4155550199", prepared[0].model_dump_json())
        self.assertEqual(prepared[0].role, "UNKNOWN")
        self.assertIsNone(prepared[0].confidence)

    def test_redaction_error_returns_no_partial_batch_and_no_secret_in_error(self):
        secret = "secret name"
        segments = [
            {"segment_id": "a", "speaker_id": "mono", "role": "UNKNOWN", "start_ms": 0, "end_ms": 1, "text": "safe", "confidence": None},
            {"segment_id": "b", "speaker_id": "mono", "role": "UNKNOWN", "start_ms": 1, "end_ms": 2, "text": secret, "confidence": None},
        ]
        def failing_redactor(text):
            if text == secret:
                raise RuntimeError("redactor exception includes source")
            return text
        with self.assertRaises(TranscriptionError) as raised:
            prepare_utterances(segments, failing_redactor)
        self.assertNotIn(secret, str(raised.exception))

    def test_local_adapter_uses_bounded_fake_model_and_private_storage(self):
        model = FakeModel([SimpleNamespace(start=0.0, end=0.5, text="hello")])
        result = transcribe_recording("call.audio", "en", storage=FakeStorage(), model=model, duration_ms=500)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "UNKNOWN")
        self.assertEqual(model.received, b"synthetic audio")
        self.assertEqual(model.options["word_timestamps"], False)
        self.assertTrue(model.options["vad_filter"])

    def test_transcription_rejects_duration_over_cap_before_model_use(self):
        with self.assertRaises(TranscriptionError):
            transcribe_recording("call.audio", "en", storage=FakeStorage(), model=FakeModel([]), duration_ms=120 * 60 * 1000 + 1)

    def test_local_adapter_rejects_audio_checksum_mismatch(self):
        with self.assertRaises(TranscriptionError):
            transcribe_recording("call.audio", "en", storage=FakeStorage(), model=FakeModel([]), expected_sha256="0" * 64)

    def test_job_handler_only_returns_redacted_persistence_payload(self):
        fake_connection = MagicMock()
        fake_connection.__enter__.return_value = fake_connection
        fake_connection.execute.return_value.fetchone.return_value = ("en", "call.audio", 500, "a" * 64)
        raw_secret = "contact me at jane@example.com"
        with patch("app.transcription.connect", return_value=fake_connection):
            process = make_transcription_processor(
                storage=FakeStorage(),
                transcriber=lambda *args, **kwargs: [{"segment_id": "seg-1", "speaker_id": "mono", "role": "UNKNOWN", "start_ms": 0, "end_ms": 500, "text": raw_secret, "confidence": None}],
                redact=lambda text: text.replace("jane@example.com", "[REDACTED]"),
                model_version="local-model@revision-sha",
            )
            result = process({"organisation_id": "org", "call_id": "call", "id": "job"})
        encoded = str(result)
        self.assertNotIn("jane@example.com", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertEqual(result["processing_state"], "ANALYSING")


class ArtifactVerificationTests(unittest.TestCase):
    def test_model_directory_requires_exact_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.bin").write_bytes(b"synthetic model bytes")
            digest = directory_sha256(root)
            self.assertEqual(verified_model_directory(str(root), digest), root.resolve())
            with self.assertRaises(RuntimeError):
                verified_model_directory(str(root), hashlib.sha256(b"wrong").hexdigest())
            with self.assertRaises(RuntimeError):
                verified_model_directory("relative/model", digest)


class TranscriptMigrationTests(unittest.TestCase):
    def test_v3_schema_upgrade_adds_revision_and_redacted_utterance_table(self):
        # Models an already-migrated deployment upgrading from the prior calls shape.
        from pathlib import Path
        migration = Path(__file__).resolve().parent.parent / "migrations" / "004_redacted_transcripts.sql"
        schema = f"migration_upgrade_{uuid4().hex}"
        with connect() as connection:
            try:
                connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                connection.execute("CREATE TABLE calls(organisation_id uuid NOT NULL,id uuid NOT NULL,PRIMARY KEY(organisation_id,id))")
                connection.execute(migration.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO calls(organisation_id,id) VALUES (%s,%s)", (uuid4(), uuid4()))
                self.assertEqual(connection.execute("SELECT transcript_revision FROM calls").fetchone()[0], 0)
                self.assertIsNotNone(connection.execute("SELECT to_regclass('transcript_utterances')").fetchone()[0])
            finally:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@unittest.skipUnless(os.environ.get("DATABASE_URL") or os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class TranscriptPersistenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        database_url = os.environ.get("DATABASE_URL")
        if not database_url or not (urlparse(database_url).path or "").lstrip("/").endswith("_test"):
            raise RuntimeError("Refusing transcript integration test unless DATABASE_URL ends in _test")
        migrate()
        self.organisation_id, self.call_id, self.job_id, self.lease_token = uuid4(), uuid4(), uuid4(), uuid4()
        self.created_organisation = False
        with connect() as connection:
            self.created_organisation = connection.execute("INSERT INTO organisations(id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING id", (self.organisation_id,)).fetchone() is not None
            connection.execute("INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'TRANSCRIBING')", (self.organisation_id, self.call_id, f"transcript-{self.call_id}", f"transcript-{self.call_id}", "0" * 64, "agent", "team", "en"))
            connection.execute("INSERT INTO jobs(organisation_id,id,call_id,stage,state,attempts,lease_token,lease_until) VALUES (%s,%s,%s,'TRANSCRIBE','RUNNING',1,%s,now()+interval '1 minute')", (self.organisation_id, self.job_id, self.call_id, self.lease_token))

    def tearDown(self):
        with connect() as connection:
            connection.execute("DELETE FROM transcript_utterances WHERE organisation_id=%s AND call_id=%s", (self.organisation_id, self.call_id))
            connection.execute("DELETE FROM jobs WHERE organisation_id=%s AND call_id=%s", (self.organisation_id, self.call_id))
            connection.execute("DELETE FROM calls WHERE organisation_id=%s AND id=%s", (self.organisation_id, self.call_id))
            if self.created_organisation:
                connection.execute("DELETE FROM organisations WHERE id=%s", (self.organisation_id,))

    def test_final_redacted_utterances_commit_with_job_and_raw_fields_are_rejected(self):
        row = {
            "id": "utterance-1", "segment_id": "segment-1", "speaker_id": "mono-unknown",
            "role": "UNKNOWN", "start_ms": 0, "end_ms": 500,
            "text_redacted": "My email is [REDACTED]", "confidence": None,
            "is_final": True,
        }
        with connect() as connection:
            with self.assertRaises(ValidationError):
                finish_job(connection, str(self.job_id), str(self.lease_token), {
                    "processing_state": "ANALYSING", "model_version": "local@sha256", "utterances": [{**row, "text": "private@example.com"}],
                })
            self.assertEqual(connection.execute("SELECT state FROM jobs WHERE id=%s", (self.job_id,)).fetchone()[0], "RUNNING")
            self.assertEqual(connection.execute("SELECT count(*) FROM transcript_utterances WHERE call_id=%s", (self.call_id,)).fetchone()[0], 0)
            self.assertTrue(finish_job(connection, str(self.job_id), str(self.lease_token), {
                "processing_state": "ANALYSING", "model_version": "local@sha256", "utterances": [row],
            }))
            persisted = connection.execute("SELECT text_redacted,is_final,revision FROM transcript_utterances WHERE call_id=%s", (self.call_id,)).fetchone()
            self.assertEqual(persisted, ("My email is [REDACTED]", True, 1))
            self.assertEqual(connection.execute("SELECT transcript_revision FROM calls WHERE id=%s", (self.call_id,)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT stage,state,input_revision FROM jobs WHERE call_id=%s AND stage='ANALYSE'", (self.call_id,)).fetchone(), ("ANALYSE", "QUEUED", 1))

    def test_expired_lease_cannot_commit_transcript(self):
        with connect() as connection:
            connection.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (self.job_id,))
            self.assertFalse(finish_job(connection, str(self.job_id), str(self.lease_token), {
                "processing_state": "ANALYSING", "model_version": "local@sha256", "utterances": [],
            }))
            self.assertEqual(connection.execute("SELECT count(*) FROM transcript_utterances WHERE call_id=%s", (self.call_id,)).fetchone()[0], 0)


class WorkerPrivacyBoundaryTests(unittest.TestCase):
    @patch("app.worker.finish_job")
    @patch("app.worker.retry_job", return_value=True)
    @patch("app.worker.claim_job", return_value={"id": "job", "lease_token": "lease", "stage": "TRANSCRIBE", "organisation_id": "org", "call_id": "call"})
    @patch("app.worker.connect")
    @patch("app.transcription.connect")
    def test_redaction_failure_retries_without_finishing_or_logging_text(self, transcribe_connect, worker_connect, claim, retry, finish):
        import logging
        from app.worker import run_once

        db = MagicMock()
        transcribe_connect.return_value.__enter__.return_value = db
        db.execute.return_value.fetchone.return_value = ("en", "call.audio", 500, "a" * 64)
        secret = "private@example.com"
        process = make_transcription_processor(
            transcriber=lambda *args, **kwargs: [{"segment_id": "s1", "speaker_id": "mono", "role": "UNKNOWN", "start_ms": 0, "end_ms": 1, "text": secret, "confidence": None}],
            redact=lambda _text: (_ for _ in ()).throw(RuntimeError("redaction rejected")),
            model_version="local@sha256",
        )
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            self.assertTrue(run_once("worker-a", {"TRANSCRIBE": process}))
        finally:
            root_logger.removeHandler(handler)
        retry.assert_called_once()
        finish.assert_not_called()
        self.assertNotIn(secret, sink.getvalue())


if __name__ == "__main__":
    unittest.main()
