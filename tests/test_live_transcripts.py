"""Focused contracts for short-lived provisional live transcript storage."""

import unittest
from datetime import datetime, timedelta, timezone
import os
from urllib.parse import urlparse
from uuid import uuid4
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.auth import Scope
from app.api import create_app
from app.config import Settings
from app.live_calls import LiveCallsForbidden
from app.live_transcripts import (LiveTranscriptUnavailable, read_live_utterances,
                                  purge_expired_live_utterances, store_live_utterances,
                                  LIVE_TRANSCRIPT_SWEEP_BATCH, update_live_transcription_state)


class _Result:
    def __init__(self, one=None, many=(), rowcount=0):
        self.one, self.many, self.rowcount = one, list(many), rowcount

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _Connection:
    def __init__(self, *, agent_id="agent-a", team_id="team-a", generation=3,
                 state="LIVE", tombstoned_at=None, call_content_purged_at=None, expires_at=None):
        self.agent_id, self.team_id = agent_id, team_id
        self.generation, self.state, self.tombstoned_at = generation, state, tombstoned_at
        self.call_content_purged_at = call_content_purged_at
        self.expires_at = expires_at or (datetime.now(timezone.utc) + timedelta(minutes=5))
        self.rows = []
        self.queries = []
        self.transcription_state = "EMPTY"
        self.truncated = False

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if query.startswith("WITH expired AS"):
            return _Result(rowcount=3)
        if query.startswith("WITH excess AS"):
            keep = params[4]
            ordered = sorted(self.rows, key=lambda row: (row["start_ms"], row["id"]), reverse=True)
            removed = {row["id"] for row in ordered[keep:]}
            self.rows = [row for row in self.rows if row["id"] not in removed]
            return _Result(rowcount=len(removed))
        if "SELECT s.state,s.call_content_purged_at" in query:
            if params[0] != "org-a" or params[1] != "integration-a" or params[2] != "a" * 64 or params[3] != self.generation:
                return _Result()
            return _Result((self.state, self.call_content_purged_at))
        if "SELECT tombstoned_at FROM calls" in query:
            if params[0] == "org-a" and params[1] == "a" * 64 and self.tombstoned_at is not None:
                return _Result((self.tombstoned_at,))
            return _Result()
        if query.startswith("DELETE FROM live_transcript_utterances"):
            self.rows = [r for r in self.rows if r["expires_at"] > datetime.now(timezone.utc)]
            return _Result(rowcount=0)
        if "SELECT count(*) FROM live_transcript_utterances" in query:
            return _Result((len(self.rows),))
        if "SELECT utterance_id FROM live_transcript_utterances" in query:
            return _Result(many=[(r["id"],) for r in self.rows])
        if query.startswith("INSERT INTO live_transcript_utterances"):
            _, _, _, generation, uid, start, end, role, redacted, _ttl = params
            self.rows = [r for r in self.rows if r["id"] != uid]
            self.rows.append({"id": uid, "generation": generation, "start_ms": start,
                              "end_ms": end, "role": role, "text_redacted": redacted,
                              "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15)})
            return _Result(rowcount=1)
        if query.startswith("UPDATE exotel_sessions SET live_transcript_truncated=true"):
            self.truncated = True
            return _Result((self.generation,))
        if query.startswith("UPDATE exotel_sessions SET live_transcription_state=CASE"):
            if self.transcription_state != "DEGRADED":
                self.transcription_state = "LIVE"
            return _Result((self.generation,))
        if query.startswith("UPDATE exotel_sessions SET live_transcription_state="):
            if self.state not in {"LIVE", "DRAINING"}:
                return _Result()
            self.transcription_state = params[0]
            return _Result((self.generation,))
        if query.startswith("SELECT u.utterance_id"):
            org, key, generation, *extra = params
            role = "TEAM_LEADER" if "team_id=ANY" in query else "AGENT" if "agent_id=%s" in query else "QA_ANALYST"
            user = "agent-a" if role == "AGENT" else "reviewer"
            teams = set(extra[0]) if role == "TEAM_LEADER" else set()
            if (org != "org-a" or key != "a" * 64 or generation != self.generation
                    or self.state not in {"LIVE", "DRAINING", "ENDED", "INCOMPLETE"} or self.tombstoned_at is not None
                    or self.call_content_purged_at is not None):
                return _Result(many=[])
            if role == "AGENT" and extra[0] != self.agent_id:
                return _Result(many=[])
            if role == "TEAM_LEADER" and self.team_id not in teams:
                return _Result(many=[])
            active = [(r["id"], r["role"], r["start_ms"], r["end_ms"], r["text_redacted"], self.agent_id, self.team_id,
                       self.transcription_state, self.truncated)
                      for r in sorted(self.rows, key=lambda row: row["start_ms"])
                      if r["generation"] == generation and r["expires_at"] > datetime.now(timezone.utc)]
            return _Result(many=active or [(None, None, None, None, None, self.agent_id, self.team_id,
                                            self.transcription_state, self.truncated)])
        raise AssertionError(f"unexpected SQL: {query}")


class LiveTranscriptTests(unittest.TestCase):
    @staticmethod
    def utterance(text="hello"):
        return {"id": "a" * 32, "role": "UNKNOWN", "start_ms": 10, "end_ms": 20, "text": text}

    def test_storage_redacts_and_limits_provisional_input(self):
        connection = _Connection()
        count = store_live_utterances(connection, "org-a", "integration-a", "a" * 64, 3,
                                      [self.utterance(text="call me at 5551234567")],
                                      redactor=lambda text: text.replace("5551234567", "[REDACTED]"))
        self.assertEqual(count, 1)
        self.assertEqual(connection.rows[0]["text_redacted"], "call me at [REDACTED]")
        self.assertFalse(any("text" in str(params) and "5551234567" in str(params) for _, params in connection.queries if "INSERT" in _))
        locks = [query for query, _ in connection.queries if "FOR UPDATE" in query]
        self.assertIn("FROM exotel_sessions", locks[0])
        self.assertIn("FROM calls", locks[1])
        with self.assertRaises(ValueError):
            store_live_utterances(connection, "org-a", "integration-a", "a" * 64, 3,
                                  [{**self.utterance(), "start_ms": True}], redactor=str)
        with self.assertRaises(ValueError):
            store_live_utterances(connection, "org-a", "integration-a", "a" * 64, 3,
                                  [{**self.utterance(), "id": "5551234567"}], redactor=str)

    def test_session_cap_drops_oldest_preview_rows_and_marks_truncated(self):
        connection = _Connection()
        for batch_start in range(0, 505, 100):
            batch = [{**self.utterance(), "id": f"{index:032x}", "start_ms": index, "end_ms": index + 10}
                     for index in range(batch_start, min(505, batch_start + 100))]
            store_live_utterances(connection, "org-a", "integration-a", "a" * 64, 3, batch, redactor=str)
        self.assertEqual(len(connection.rows), 500)
        self.assertTrue(connection.truncated)
        self.assertEqual(min(row["start_ms"] for row in connection.rows), 5)
        self.assertEqual(connection.transcription_state, "LIVE")

    def test_write_rejects_stale_generation_and_tombstoned_call(self):
        for connection in (_Connection(generation=4), _Connection(tombstoned_at=datetime.now(timezone.utc)),
                           _Connection(call_content_purged_at=datetime.now(timezone.utc))):
            with self.assertRaises(LiveTranscriptUnavailable):
                store_live_utterances(connection, "org-a", "integration-a", "a" * 64, 3,
                                      [self.utterance()], redactor=str)

    def test_read_enforces_tenant_team_agent_generation_expiry_and_tombstone(self):
        connection = _Connection()
        store_live_utterances(connection, "org-a", "integration-a", "a" * 64, 3,
                              [self.utterance()], redactor=str)
        qa = Scope("org-a", "qa", "QA_ANALYST", frozenset())
        self.assertEqual(read_live_utterances(connection, qa, "a" * 64, 3)["items"][0]["is_final"], False)
        for scope, generation in (
            (Scope("org-b", "qa", "QA_ANALYST", frozenset()), 3),
            (Scope("org-a", "leader", "TEAM_LEADER", frozenset({"team-b"})), 3),
            (Scope("org-a", "agent-b", "AGENT", frozenset({"team-a"})), 3),
            (qa, 2),
        ):
            with self.subTest(scope=scope, generation=generation), self.assertRaises(LiveTranscriptUnavailable):
                read_live_utterances(connection, scope, "a" * 64, generation)
        with self.assertRaises(LiveCallsForbidden):
            read_live_utterances(connection, Scope("org-a", "svc", "SERVICE", frozenset()), "a" * 64, 3)

        connection.rows[0]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(read_live_utterances(connection, qa, "a" * 64, 3)["items"], [])
        connection.rows[0]["expires_at"] = datetime.now(timezone.utc) + timedelta(minutes=2)
        connection.tombstoned_at = datetime.now(timezone.utc)
        with self.assertRaises(LiveTranscriptUnavailable):
            read_live_utterances(connection, qa, "a" * 64, 3)

    def test_authorized_session_without_utterances_returns_empty_list(self):
        self.assertEqual(read_live_utterances(_Connection(), Scope("org-a", "qa", "QA_ANALYST", frozenset()), "a" * 64, 3)["items"], [])

    def test_recently_ended_preview_is_readable_within_ttl(self):
        connection = _Connection(state="ENDED")
        connection.rows.append({"id": "a" * 32, "generation": 3, "start_ms": 10, "end_ms": 20,
                                "role": "UNKNOWN", "text_redacted": "redacted preview",
                                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)})
        result = read_live_utterances(connection, Scope("org-a", "qa", "QA_ANALYST", frozenset()), "a" * 64, 3)
        self.assertEqual(result["items"][0]["text_redacted"], "redacted preview")
        query = next(q for q, _params in connection.queries if q.startswith("SELECT u.utterance_id"))
        self.assertIn("s.state IN ('LIVE','DRAINING','ENDED','INCOMPLETE')", query)

    def test_status_mutations_are_generation_and_lifecycle_fenced(self):
        connection = _Connection()
        self.assertTrue(update_live_transcription_state(connection, "org-a", "integration-a", "a" * 64, 3, "DEGRADED"))
        query, params = connection.queries[-1]
        self.assertIn("state IN ('LIVE','DRAINING')", query)
        self.assertIn("call_content_purged_at IS NULL", query)
        self.assertIn("tombstoned_at IS NOT NULL", query)
        self.assertEqual(params, ("DEGRADED", "org-a", "integration-a", "a" * 64, 3, "DEGRADED"))
        ended = _Connection(state="ENDED")
        self.assertFalse(update_live_transcription_state(ended, "org-a", "integration-a", "a" * 64, 3, "DEGRADED"))

    def test_expiry_sweep_uses_bounded_skip_locked_delete(self):
        connection = _Connection()
        self.assertEqual(purge_expired_live_utterances(connection), 3)
        query, params = connection.queries[-1]
        self.assertIn("LIMIT %s FOR UPDATE SKIP LOCKED", query)
        self.assertEqual(params, (LIVE_TRANSCRIPT_SWEEP_BATCH,))
        with self.assertRaises(ValueError):
            purge_expired_live_utterances(connection, limit=LIVE_TRANSCRIPT_SWEEP_BATCH + 1)

    def test_read_route_returns_generation_scoped_items(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        settings = Settings(oidc_issuer="https://identity.test", oidc_audience="audit", oidc_public_key=public_key,
                            csrf_secret="test-only-csrf-secret-at-least-32-bytes-long", allowed_origins=("https://audit.test",))
        scope = Scope("org-a", "qa", "QA_ANALYST", frozenset())
        app = create_app(settings, identity_lookup=lambda _subject: scope)
        token = jwt.encode({"sub": "qa", "iss": settings.oidc_issuer, "aud": settings.oidc_audience,
                            "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, key, algorithm="RS256")
        connection = _Connection()
        store_live_utterances(connection, "org-a", "integration-a", "a" * 64, 3, [self.utterance()], redactor=str)
        with TestClient(app) as client, patch("app.api.connect", return_value=connection):
            response = client.get("/v1/live-calls/" + "a" * 64 + "/utterances?generation=3",
                                  headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "LIVE", "truncated": False,
                                           "items": [{"id": "a" * 32, "role": "UNKNOWN", "start_ms": 10,
                                                      "end_ms": 20, "text_redacted": "hello", "is_final": False}]})

    def test_team_leader_can_read_only_server_resolved_team(self):
        connection = _Connection()
        store_live_utterances(connection, "org-a", "integration-a", "a" * 64, 3,
                              [self.utterance()], redactor=str)
        self.assertEqual(len(read_live_utterances(connection, Scope("org-a", "leader", "TEAM_LEADER", frozenset({"team-a"})), "a" * 64, 3)["items"]), 1)
        query, params = connection.queries[-1]
        self.assertIn("team_id=ANY(%s)", query)
        self.assertEqual(params[-1], ["team-a"])


@unittest.skipUnless(os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class LiveTranscriptPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("DATABASE_URL") or not (urlparse(os.environ["DATABASE_URL"]).path or "").lstrip("/").endswith("_test"):
            raise RuntimeError("Refusing live transcript integration tests without isolated DATABASE_URL ending in _test")
        from app.migrate import migrate

        migrate()
        from app.db import connect

        with connect() as connection:
            if connection.execute("SELECT 1 FROM schema_migrations WHERE version=20").fetchone() is None:
                raise RuntimeError("Migration 020 was not applied to the isolated test database")

    def setUp(self):
        from app.db import connect

        self.connect = connect
        self.organisation_id, self.integration_id = uuid4(), uuid4()
        self.call_key = uuid4().hex + uuid4().hex
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s)", (self.organisation_id,))
            connection.execute(
                "INSERT INTO exotel_integrations(organisation_id,id,account_sid,username,password_salt,password_verifier,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (self.organisation_id, self.integration_id, f"acct-{self.integration_id}", f"user-{self.integration_id}",
                 b"s" * 16, b"v" * 32, "integration-test"),
            )
            connection.execute(
                "INSERT INTO exotel_sessions(organisation_id,integration_id,call_key,generation,state,agent_id,team_id) "
                "VALUES (%s,%s,%s,1,'LIVE','agent-test','team-test')",
                (self.organisation_id, self.integration_id, self.call_key),
            )

    def tearDown(self):
        with self.connect() as connection:
            connection.execute("DELETE FROM exotel_sessions WHERE organisation_id=%s AND integration_id=%s", (self.organisation_id, self.integration_id))
            connection.execute("DELETE FROM exotel_integrations WHERE organisation_id=%s AND id=%s", (self.organisation_id, self.integration_id))
            connection.execute("DELETE FROM organisations WHERE id=%s", (self.organisation_id,))

    def test_migration_020_status_read_and_session_cap_eviction(self):
        scope = Scope(str(self.organisation_id), "qa-test", "QA_ANALYST", frozenset())
        with self.connect() as connection:
            self.assertTrue(update_live_transcription_state(connection, str(self.organisation_id), str(self.integration_id),
                                                            self.call_key, 1, "EMPTY"))
            for batch_start in range(0, 505, 100):
                batch = [{"id": f"{index:032x}", "role": "UNKNOWN", "start_ms": index,
                          "end_ms": index + 1, "text": f"preview {index}"}
                         for index in range(batch_start, min(batch_start + 100, 505))]
                store_live_utterances(connection, str(self.organisation_id), str(self.integration_id),
                                      self.call_key, 1, batch, redactor=str)
            result = read_live_utterances(connection, scope, self.call_key, 1)
            self.assertEqual(result["status"], "LIVE")
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["items"]), 500)
            self.assertEqual(min(item["start_ms"] for item in result["items"]), 5)
            stored = connection.execute(
                "SELECT count(*) FROM live_transcript_utterances WHERE organisation_id=%s AND integration_id=%s AND call_key=%s",
                (self.organisation_id, self.integration_id, self.call_key),
            ).fetchone()[0]
            self.assertEqual(stored, 500)


if __name__ == "__main__":
    unittest.main()
