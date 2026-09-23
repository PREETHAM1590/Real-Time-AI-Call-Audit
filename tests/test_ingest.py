import io
import os
import tempfile
import unittest
import wave
from urllib.parse import urlparse
from uuid import uuid4

from app.auth import Scope
from app.db import connect
from app.ingest import IdempotencyConflict, IntakeError, accept_recording, claim_job, finish_job, inspect_audio
from app.migrate import migrate
from app.storage import LocalPrivateStorage


def wav_fixture() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(b"\0\0" * 1600)
    return buffer.getvalue()


class AudioValidationTests(unittest.TestCase):
    def test_valid_synthetic_wav_and_spoof_rejected(self):
        self.assertEqual(inspect_audio(wav_fixture()), ("wav", 16000, 1, 100))
        with self.assertRaises(IntakeError):
            inspect_audio(b"RIFF" + b"not an audio file")


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL required for isolated PostgreSQL integration")
class PostgresIngestTests(unittest.TestCase):
    def setUp(self):
        parsed = urlparse(os.environ["DATABASE_URL"])
        if not (parsed.path or "").lstrip("/").endswith("_test"):
            raise RuntimeError("Refusing integration tests unless DATABASE_URL database name ends in _test")
        migrate()
        self.organisation_id, self.call_id = uuid4(), uuid4()
        self.scope = Scope(str(self.organisation_id), "agent-test", "AGENT", frozenset())
        self.storage_dir = tempfile.TemporaryDirectory()
        self.storage = LocalPrivateStorage(self.storage_dir.name)
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s) ON CONFLICT DO NOTHING", (self.organisation_id,))

    def tearDown(self):
        with connect() as connection:
            connection.execute("DELETE FROM organisations WHERE id=%s AND NOT EXISTS (SELECT 1 FROM calls WHERE organisation_id=%s)", (self.organisation_id, self.organisation_id))
        self.storage_dir.cleanup()

    def test_intake_idempotency_conflict_and_lease_recovery(self):
        data = wav_fixture()
        first = accept_recording(self.scope, "ext-1", data, {"agent_id": "agent-test", "team_id": "team-test"}, "idem-1", storage=self.storage)
        again = accept_recording(self.scope, "ext-1", data, {"agent_id": "agent-test", "team_id": "team-test"}, "idem-1", storage=self.storage)
        self.assertEqual(first, again)
        with self.assertRaises(IdempotencyConflict):
            accept_recording(self.scope, "ext-1", data + b"x", {"agent_id": "agent-test", "team_id": "team-test"}, "idem-1", storage=self.storage)
        with connect() as connection:
            one = claim_job(connection, "worker-a")
            self.assertIsNotNone(one)
            self.assertIsNone(claim_job(connection, "worker-b"))
            connection.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (one["id"],))
            two = claim_job(connection, "worker-b")
            self.assertEqual(one["id"], two["id"])
            self.assertFalse(finish_job(connection, str(one["id"]), str(one["lease_token"]), {}))
            self.assertTrue(finish_job(connection, str(two["id"]), str(two["lease_token"]), {}))


if __name__ == "__main__":
    unittest.main()
