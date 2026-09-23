import unittest
import os
from urllib.parse import urlparse
from uuid import uuid4

from app.audio_access import issue_audio_capability, verify_audio_capability
from app.auth import Scope
from app.db import connect
from app.migrate import migrate


class AudioCapabilityTests(unittest.TestCase):
    def test_capability_is_bound_to_identity_call_and_sixty_second_expiry(self):
        qa = Scope("org-1", "qa-1", "QA_ANALYST", frozenset())
        token = issue_audio_capability(qa, "call-1", "audio-1", "test-secret", now=100)
        self.assertEqual(verify_audio_capability(token, qa, "call-1", "test-secret", now=159), "audio-1")
        self.assertIsNone(verify_audio_capability(token, qa, "call-1", "test-secret", now=160))
        self.assertIsNone(verify_audio_capability(token, Scope("org-2", "qa-1", "QA_ANALYST", frozenset()), "call-1", "test-secret", now=101))
        self.assertIsNone(verify_audio_capability(token, Scope("org-1", "qa-2", "QA_ANALYST", frozenset()), "call-1", "test-secret", now=101))
        self.assertIsNone(verify_audio_capability(token, qa, "call-2", "test-secret", now=101))
        self.assertIsNone(verify_audio_capability(token, qa, "call-1", "wrong-secret", now=101))


@unittest.skipUnless(os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class AudioAccessMigrationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database = (urlparse(os.environ.get("DATABASE_URL", "")).path or "").lstrip("/")
        if not database.endswith("_test"):
            raise RuntimeError("Refusing access-event integration test without isolated DATABASE_URL ending in _test")
        migrate()

    def test_audio_grant_event_is_supported_and_immutable(self):
        organisation_id, event_id, audio_id = uuid4(), uuid4(), uuid4()
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s)", (organisation_id,))
            connection.execute(
                "INSERT INTO access_events(organisation_id,id,actor_id,action,resource_type,resource_id,outcome) "
                "VALUES (%s,%s,'qa-test','AUDIO_ACCESS_GRANTED','AUDIO_OBJECT',%s,'SUCCESS')",
                (organisation_id, event_id, audio_id),
            )
            for statement in (
                "UPDATE access_events SET outcome='FAILURE' WHERE organisation_id=%s AND id=%s",
                "DELETE FROM access_events WHERE organisation_id=%s AND id=%s",
            ):
                with self.assertRaises(Exception), connection.transaction():
                    connection.execute(statement, (organisation_id, event_id))
