import io
import os
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4
from unittest.mock import patch

from app.auth import Scope
from app.db import connect
from app.ingest import IdempotencyConflict, IntakeError, accept_recording, claim_job, defer_job, finish_job, inspect_audio, renew_job
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
        with self.assertRaisesRegex(IntakeError, "Truncated"):
            inspect_audio(wav_fixture()[:-2])
        with self.assertRaises(IntakeError):
            inspect_audio(b"RIFF" + b"not an audio file")

    @patch("app.ingest.subprocess.run")
    def test_mp3_probe_uses_closed_temp_file_and_cleans_it(self, run):
        def fake_run(args, **kwargs):
            self.assertNotIn("shell", kwargs)
            self.assertTrue(Path(args[args.index("-i") + 1] if "-i" in args else args[-1]).exists())
            if "-count_frames" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps({"streams": [{"sample_rate": "16000", "channels": 1, "nb_read_frames": "28"}], "format": {"duration": "1.0"}}).encode(), b"")
            return subprocess.CompletedProcess(args, 0, b"", b"")

        run.side_effect = fake_run
        before = set(Path(tempfile.gettempdir()).glob("tmp*.mp3"))
        self.assertEqual(inspect_audio(b"ID3" + b"synthetic"), ("mp3", 16000, 1, 1000))
        args = run.call_args_list
        temp_path = args[0].args[0][-1]
        self.assertEqual(len(args), 2)
        self.assertFalse(Path(temp_path).exists())
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("tmp*.mp3")), before)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for full-decode truncation check")
    def test_mp3_truncation_is_rejected_by_full_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory, "synthetic.mp3")
            subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-codec:a", "libmp3lame", "-y", str(audio_path)], check=True, capture_output=True)
            audio = audio_path.read_bytes()
        self.assertGreater(inspect_audio(audio)[3], 0)
        for proportion in (0.9, 0.75, 0.5):
            with self.subTest(proportion=proportion), self.assertRaises(IntakeError):
                inspect_audio(audio[:int(len(audio) * proportion)])

    def test_intake_requires_agent_identity_and_resolved_team(self):
        data = wav_fixture()
        for scope in (
            Scope("org-a", "agent-a", "AGENT", frozenset()),
            Scope("org-a", "", "AGENT", frozenset({"team-a"})),
            Scope("org-a", "agent-a", "AGENT", frozenset({" "})),
            Scope("org-a", "staff-a", "QA_ANALYST", frozenset({"team-a"})),
        ):
            with self.subTest(scope=scope), self.assertRaises(IntakeError):
                accept_recording(scope, "external", data, {}, "idem")


@unittest.skipUnless(os.environ.get("DATABASE_URL") or os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class PostgresIngestTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("DATABASE_URL"):
            raise RuntimeError("RUN_POSTGRES_INTEGRATION=1 requires DATABASE_URL for an isolated PostgreSQL database ending in _test")
        parsed = urlparse(os.environ["DATABASE_URL"])
        if not (parsed.path or "").lstrip("/").endswith("_test"):
            raise RuntimeError("Refusing integration tests unless DATABASE_URL database name ends in _test")
        migrate()
        self.organisation_id, self.call_id = uuid4(), uuid4()
        self.scope = Scope(str(self.organisation_id), "agent-test", "AGENT", frozenset({"team-test"}))
        self.storage_dir = tempfile.TemporaryDirectory()
        self.storage = LocalPrivateStorage(self.storage_dir.name)
        with connect() as connection:
            self.created_organisation = connection.execute("INSERT INTO organisations(id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING id", (self.organisation_id,)).fetchone() is not None
        self.created_call_ids = set()

    def tearDown(self):
        if self.created_call_ids:
            with connect() as connection:
                call_ids = [UUID(value) for value in self.created_call_ids]
                connection.execute("DELETE FROM jobs WHERE organisation_id=%s AND call_id=ANY(%s)", (self.organisation_id, call_ids))
                connection.execute("DELETE FROM audio_objects WHERE organisation_id=%s AND call_id=ANY(%s)", (self.organisation_id, call_ids))
                connection.execute("DELETE FROM events WHERE organisation_id=%s AND call_id=ANY(%s)", (self.organisation_id, call_ids))
                connection.execute("DELETE FROM calls WHERE organisation_id=%s AND id=ANY(%s)", (self.organisation_id, call_ids))
                connection.execute("DELETE FROM event_counters WHERE organisation_id=%s", (self.organisation_id,))
        if self.created_organisation:
            with connect() as connection:
                connection.execute("DELETE FROM organisations WHERE id=%s", (self.organisation_id,))
        self.storage_dir.cleanup()

    def test_intake_idempotency_conflict_and_lease_recovery(self):
        data = wav_fixture()
        first = accept_recording(self.scope, "ext-1", data, {"agent_id": "agent-test", "team_id": "team-test"}, "idem-1", storage=self.storage)
        self.created_call_ids.add(first["id"])
        again = accept_recording(self.scope, "ext-1", data, {"agent_id": "agent-test", "team_id": "team-test"}, "idem-1", storage=self.storage)
        self.assertEqual(first, again)
        with ThreadPoolExecutor(max_workers=2) as pool:
            raced = list(pool.map(lambda _: accept_recording(self.scope, "ext-1", data, {}, "idem-1", storage=self.storage), range(2)))
        self.assertEqual(raced, [first, first])
        with self.assertRaises(IdempotencyConflict):
            accept_recording(self.scope, "ext-1", data + b"x", {"agent_id": "agent-test", "team_id": "team-test"}, "idem-1", storage=self.storage)
        with connect() as connection_a, connect() as connection_b:
            self.assertIsNone(claim_job(connection_a, "worker-a", supported_stages=()))
            for _ in range(6):
                one = claim_job(connection_a, "worker-a", supported_stages=("TRANSCRIBE",))
                self.assertIsNotNone(one)
                self.assertTrue(renew_job(connection_a, str(one["id"]), str(one["lease_token"])))
                self.assertTrue(defer_job(connection_a, str(one["id"]), str(one["lease_token"])))
                attempts = connection_a.execute("SELECT attempts FROM jobs WHERE organisation_id=%s AND id=%s", (self.organisation_id, one["id"])).fetchone()[0]
                self.assertEqual(attempts, 0)
                connection_a.commit()
            self.assertIsNone(claim_job(connection_b, "worker-b", supported_stages=("ANALYSE",)))
            connection_b.commit()
            self.assertIsNone(claim_job(connection_b, "worker-b", supported_stages=()))
            two = claim_job(connection_b, "worker-b", supported_stages=("TRANSCRIBE",))
            self.assertEqual(one["id"], two["id"])
            self.assertFalse(finish_job(connection_a, str(one["id"]), str(one["lease_token"]), {}))
            connection_a.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE organisation_id=%s AND id=%s", (self.organisation_id, one["id"]))
            connection_a.commit()
            three = claim_job(connection_a, "worker-a", supported_stages=("TRANSCRIBE",))
            self.assertEqual(one["id"], three["id"])
            self.assertFalse(finish_job(connection_b, str(two["id"]), str(two["lease_token"]), {}))
            connection_b.execute("UPDATE calls SET tombstoned_at=now() WHERE organisation_id=%s AND id=%s", (self.organisation_id, first["id"]))
            connection_b.commit()
            self.assertFalse(finish_job(connection_a, str(three["id"]), str(three["lease_token"]), {}))

    def test_tombstone_does_not_starve_next_job(self):
        one = accept_recording(self.scope, "ext-1", wav_fixture(), {}, "idem-1", storage=self.storage)
        two = accept_recording(self.scope, "ext-2", wav_fixture(), {}, "idem-2", storage=self.storage)
        self.created_call_ids.update((one["id"], two["id"]))
        with connect() as connection:
            connection.execute("UPDATE calls SET tombstoned_at=now() WHERE organisation_id=%s AND id=%s", (self.organisation_id, one["id"]))
            claimed = claim_job(connection, "worker")
            self.assertEqual(str(claimed["call_id"]), two["id"])


class StorageCleanupTests(unittest.TestCase):
    def test_only_old_unreferenced_objects_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPrivateStorage(directory)
            orphan = Path(directory, "orphan.audio")
            retained = Path(directory, "retained.audio")
            orphan.write_bytes(b"orphan")
            retained.write_bytes(b"retained")
            old = (Path(directory).stat().st_mtime - 2 * 86400)
            os.utime(orphan, (old, old))
            os.utime(retained, (old, old))
            self.assertEqual(storage.delete_orphans({"retained.audio"}), 1)
            self.assertFalse(orphan.exists())
            self.assertTrue(retained.exists())


@unittest.skipUnless(os.environ.get("DATABASE_URL") or os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class MigrationUpgradeTests(unittest.TestCase):
    def test_existing_v1_jobs_constraint_accepts_waiting_handler_after_v2(self):
        if not os.environ.get("DATABASE_URL"):
            self.fail("RUN_POSTGRES_INTEGRATION=1 requires an isolated DATABASE_URL ending in _test")
        parsed = urlparse(os.environ["DATABASE_URL"])
        if not (parsed.path or "").lstrip("/").endswith("_test"):
            self.fail("Refusing integration test unless DATABASE_URL database name ends in _test")
        migration = Path(__file__).resolve().parent.parent / "migrations" / "002_waiting_handler.sql"
        with connect() as connection:
            connection.execute("CREATE TEMP TABLE jobs (state text NOT NULL CONSTRAINT jobs_state_check CHECK (state IN ('QUEUED','RUNNING','DONE','RETRY_WAIT','FAILED'))) ON COMMIT PRESERVE ROWS")
            connection.execute(migration.read_text(encoding="utf-8"))
            connection.execute("INSERT INTO jobs(state) VALUES ('WAITING_HANDLER')")
            self.assertEqual(connection.execute("SELECT state FROM jobs").fetchone()[0], "WAITING_HANDLER")


if __name__ == "__main__":
    unittest.main()
