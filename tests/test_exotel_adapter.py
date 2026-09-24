import base64
import io
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

from app.auth import Scope
from app.exotel_adapter import (
    build_wav,
    integration_credentials,
    make_audio_references,
    verify_integration_secret,
)
from app.ingest import IntakeError, accept_recording
from app.storage import LocalPrivateStorage


class ExotelAdapterTests(unittest.TestCase):
    def test_generated_secret_is_verified_without_persisting_plaintext(self):
        username, password, salt, verifier = integration_credentials()
        self.assertTrue(username)
        self.assertNotEqual(password.encode(), salt)
        self.assertNotEqual(password.encode(), verifier)
        self.assertTrue(verify_integration_secret(password, salt, verifier))
        self.assertFalse(verify_integration_secret(password + "x", salt, verifier))

    def test_recording_is_wav_and_reference_is_deterministic_and_scoped(self):
        pcm = b"\x01\x00" * 800
        audio = build_wav(pcm)
        with wave.open(io.BytesIO(audio), "rb") as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (8000, 1, 2))
            self.assertEqual(wav.readframes(800), pcm)
        first = make_audio_references("org-a", "acct-a", "call-a")
        self.assertEqual(first, make_audio_references("org-a", "acct-a", "call-a"))
        self.assertNotEqual(first, make_audio_references("org-b", "acct-a", "call-a"))

    def test_final_membership_revocation_blocks_intake_and_cleans_spooled_object(self):
        class Result:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

            def fetchall(self):
                return []

        class Connection:
            def __init__(self):
                self.queries = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def transaction(self):
                return self

            def execute(self, query, params=()):
                self.queries.append(query)
                if query.startswith("SET TRANSACTION") or "set_config('statement_timeout'" in query:
                    return Result()
                if "SELECT s.generation,s.state" in query:
                    return Result((4, "DRAINING"))
                return Result()

        connection = Connection()
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPrivateStorage(directory)
            with patch("app.ingest.connect", return_value=connection) as connect:
                with self.assertRaisesRegex(IntakeError, "membership"):
                    accept_recording(
                        Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"})),
                        "external", build_wav(b"\x01\x00" * 800), {}, "idem",
                        storage=storage,
                        generation_fence=("integration-a", "a" * 64, 4, "ref-a", "agent-a", "team-a"),
                        timeout_seconds=5,
                    )
            connect.assert_called_once_with(timeout_seconds=5)
            self.assertEqual(len(connection.queries), 4)
            self.assertTrue(all("INSERT INTO" not in query for query in connection.queries))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_exotel_integration_event_trigger_rejects_updates_and_deletes(self):
        migration = Path(__file__).resolve().parents[1] / "migrations" / "017_exotel_integration_event_immutability.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("RAISE EXCEPTION 'Exotel integration history is immutable'", sql)
        self.assertIn("BEFORE UPDATE OR DELETE ON exotel_integration_events", sql)
        self.assertIn("EXECUTE FUNCTION reject_exotel_integration_event_mutation()", sql)


if __name__ == "__main__":
    unittest.main()
