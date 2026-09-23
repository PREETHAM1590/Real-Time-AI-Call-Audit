"""Tenant-scoped durable outbox and resumable SSE checks."""

import io
import json
import os
import tempfile
import unittest
import wave
from urllib.parse import urlparse
from uuid import uuid4
from unittest.mock import patch

from fastapi import HTTPException

from app.auth import Scope
from app.events import (
    EventCursorError,
    EventCursorExpired,
    EventForbidden,
    append_call_updated,
    encode_sse,
    parse_last_event_id,
    read_events,
)
from app.ingest import accept_recording, claim_job, finish_job, retry_job
from app.storage import LocalPrivateStorage


class ConnectedRequest:
    async def is_disconnected(self):
        return False


class EventPrimitiveTests(unittest.TestCase):
    def test_last_event_id_is_bounded_ascii_decimal(self):
        self.assertEqual(parse_last_event_id(None), 0)
        self.assertEqual(parse_last_event_id("00042"), 42)
        for invalid in ("", "-1", "+1", "١", "1" * 5000, "9223372036854775808"):
            with self.subTest(invalid=invalid[:24]), self.assertRaises(EventCursorError):
                parse_last_event_id(invalid)

    def test_sse_envelope_serializes_only_expected_envelope(self):
        event = {
            "sequence": 7,
            "schema_version": 1,
            "call_id": "call-id",
            "type": "call.updated",
            "occurred_at": "2026-09-23T00:00:00Z",
            "payload": {"processing_state": "QUEUED", "transcript_revision": 0},
        }
        encoded = encode_sse(event)
        self.assertIn("id: 7\nevent: call.updated\n", encoded)
        self.assertEqual(json.loads(encoded.split("data: ", 1)[1]), event)


class EventSSEEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.api import create_app
        from app.config import Settings

        settings = Settings(
            oidc_issuer="https://issuer.example.test",
            oidc_audience="call-audit-test",
            oidc_public_key="test-only-key",
            csrf_secret="test-only-csrf-secret-long-enough-for-settings",
            allowed_origins=("http://localhost:3000",),
        )
        self.app = create_app(settings, identity_lookup=lambda _subject: None)
        composed = self.app
        while not hasattr(composed, "routes") and hasattr(composed, "app"):
            composed = composed.app
        self.endpoint = next(route.endpoint for route in composed.routes if getattr(route, "path", None) == "/v1/events")
        self.scope = Scope(str(uuid4()), "qa", "QA_ANALYST", frozenset())
        self.request = ConnectedRequest()

    async def test_route_resumes_after_last_event_id_and_emits_spec_envelope(self):
        event = {"sequence": 9, "schema_version": 1, "call_id": str(uuid4()), "type": "call.updated", "occurred_at": "2026-09-23T00:00:00Z", "payload": {"processing_state": "NEEDS_REVIEW", "transcript_revision": 1}}
        with patch("app.api.connect"), patch("app.api.read_events", return_value=[event]) as read:
            response = self.endpoint(request=self.request, last_event_id="8", scope=self.scope)
            chunk = await anext(response.body_iterator)
            await response.body_iterator.aclose()
        self.assertEqual(read.call_args.args[2:], (8,))
        self.assertIn("id: 9\nevent: call.updated\n", chunk)
        self.assertEqual(json.loads(chunk.split("data: ", 1)[1]), event)

    async def test_expired_cursor_emits_reset_required_and_future_cursor_is_400(self):
        expired = EventCursorExpired(latest_sequence=12, oldest_sequence=10)
        with patch("app.api.connect"), patch("app.api.read_events", side_effect=expired):
            response = self.endpoint(request=self.request, last_event_id="2", scope=self.scope)
            chunk = await anext(response.body_iterator)
            await response.body_iterator.aclose()
        self.assertIn("id: 12\nevent: reset_required\n", chunk)
        reset = json.loads(chunk.split("data: ", 1)[1])
        self.assertEqual(reset["call_id"], None)
        self.assertEqual(reset["payload"], {"reason": "cursor_expired"})

        with patch("app.api.connect"), patch("app.api.read_events", side_effect=EventCursorError("future")):
            with self.assertRaises(HTTPException) as error:
                self.endpoint(request=self.request, last_event_id="999", scope=self.scope)
        self.assertEqual(error.exception.status_code, 400)

    async def test_route_requires_authenticated_scope_and_does_not_prefetch_next_batch(self):
        from fastapi.testclient import TestClient

        client = TestClient(self.app)
        self.assertEqual(client.get("/v1/events").status_code, 401)
        event = {"sequence": 10, "schema_version": 1, "call_id": str(uuid4()), "type": "call.updated", "occurred_at": "2026-09-23T00:00:00Z", "payload": {"processing_state": "QUEUED", "transcript_revision": 0}}
        with patch("app.api.connect"), patch("app.api.read_events", return_value=[event]) as read:
            response = self.endpoint(request=self.request, last_event_id="9", scope=self.scope)
            iterator = response.body_iterator
            chunk = await anext(iterator)
            self.assertIn("id: 10", chunk)
            self.assertEqual(read.call_count, 1)
            await iterator.aclose()


@unittest.skipUnless(os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class EventOutboxPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database = (urlparse(os.environ.get("DATABASE_URL", "")).path or "").lstrip("/")
        if not database.endswith("_test"):
            raise RuntimeError("Refusing event integration tests without isolated DATABASE_URL ending in _test")
        from app.migrate import migrate

        migrate()

    def setUp(self):
        from app.db import connect

        self.connect = connect
        self.org_a, self.org_b = str(uuid4()), str(uuid4())
        self.call_a1, self.call_a2, self.call_b = str(uuid4()), str(uuid4()), str(uuid4())
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s),(%s)", (self.org_a, self.org_b))
            self.insert_call(connection, self.org_a, self.call_a1, "agent-a", "team-a", "private-ref-a")
            self.insert_call(connection, self.org_a, self.call_a2, "agent-b", "team-b", "private-ref-b")
            self.insert_call(connection, self.org_b, self.call_b, "agent-x", "team-x", "private-ref-x")
            append_call_updated(connection, self.org_a, self.call_a1, "QUEUED", 0)
            append_call_updated(connection, self.org_a, self.call_a2, "QUEUED", 0)
            append_call_updated(connection, self.org_a, self.call_a1, "ANALYSING", 1)
            append_call_updated(connection, self.org_b, self.call_b, "QUEUED", 0)

    def tearDown(self):
        with self.connect() as connection:
            connection.execute("DELETE FROM events WHERE organisation_id=ANY(%s)", ([self.org_a, self.org_b],))
            connection.execute("DELETE FROM event_counters WHERE organisation_id=ANY(%s)", ([self.org_a, self.org_b],))
            connection.execute("DELETE FROM jobs WHERE organisation_id=ANY(%s)", ([self.org_a, self.org_b],))
            connection.execute("DELETE FROM findings WHERE organisation_id=ANY(%s)", ([self.org_a, self.org_b],))
            connection.execute("DELETE FROM transcript_utterances WHERE organisation_id=ANY(%s)", ([self.org_a, self.org_b],))
            connection.execute("DELETE FROM audio_objects WHERE organisation_id=ANY(%s)", ([self.org_a, self.org_b],))
            connection.execute("DELETE FROM calls WHERE organisation_id=ANY(%s)", ([self.org_a, self.org_b],))
            connection.execute("DELETE FROM organisations WHERE id=ANY(%s)", ([self.org_a, self.org_b],))

    @staticmethod
    def insert_call(connection, organisation_id, call_id, agent_id, team_id, external_ref, processing_state="QUEUED"):
        connection.execute(
            "INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'en',%s)",
            (organisation_id, call_id, external_ref, str(uuid4()), "a" * 64, agent_id, team_id, processing_state),
        )

    def test_org_local_order_resume_scope_and_allowlisted_payload(self):
        with self.connect() as connection:
            qa = read_events(connection, Scope(self.org_a, "qa", "QA_ANALYST", frozenset()), 0, 100)
            team = read_events(connection, Scope(self.org_a, "lead", "TEAM_LEADER", frozenset({"team-a"})), 0, 100)
            agent = read_events(connection, Scope(self.org_a, "agent-a", "AGENT", frozenset({"team-a"})), 0, 100)
            other_org = read_events(connection, Scope(self.org_b, "qa", "QA_ANALYST", frozenset()), 0, 100)
            resumed = read_events(connection, Scope(self.org_a, "qa", "QA_ANALYST", frozenset()), 1, 1)
        self.assertEqual([event["sequence"] for event in qa], [1, 2, 3])
        self.assertEqual([event["sequence"] for event in team], [1, 3])
        self.assertEqual([event["sequence"] for event in agent], [1, 3])
        self.assertEqual([event["sequence"] for event in other_org], [1])
        self.assertEqual([event["sequence"] for event in resumed], [2])
        self.assertEqual(qa[0]["type"], "call.updated")
        self.assertEqual(qa[0]["payload"], {"processing_state": "QUEUED", "transcript_revision": 0})
        self.assertEqual(set(qa[0]), {"sequence", "schema_version", "call_id", "type", "occurred_at", "payload"})
        self.assertNotIn("private-ref", json.dumps(qa))

    def test_team_and_agent_scope_excludes_other_calls_and_unknown_roles_fail(self):
        with self.connect() as connection:
            self.assertEqual(read_events(connection, Scope(self.org_a, "lead", "TEAM_LEADER", frozenset({"missing"})), 0), [])
            with self.assertRaises(EventForbidden):
                read_events(connection, Scope(self.org_a, "other", "SOMETHING", frozenset()), 0)

    def test_expired_future_and_invalid_cursors_are_distinguished(self):
        with self.connect() as connection:
            scope = Scope(self.org_a, "qa", "QA_ANALYST", frozenset())
            connection.execute("UPDATE event_counters SET oldest_sequence=3 WHERE organisation_id=%s", (self.org_a,))
            with self.assertRaises(EventCursorExpired) as error:
                read_events(connection, scope, 0)
            self.assertEqual((error.exception.latest_sequence, error.exception.oldest_sequence), (3, 3))
            self.assertEqual([event["sequence"] for event in read_events(connection, scope, 2)], [3])
            with self.assertRaises(EventCursorError):
                read_events(connection, scope, 4)
            for cursor, limit in ((True, 1), (-1, 1), (1, 0), (1, 101)):
                with self.subTest(cursor=cursor, limit=limit), self.assertRaises(ValueError):
                    read_events(connection, scope, cursor, limit)

    def test_outbox_counter_and_event_rollback_with_caller_transaction(self):
        call = str(uuid4())
        with self.connect() as connection:
            self.insert_call(connection, self.org_a, call, "agent-a", "team-a", "rollback-ref")
        with self.assertRaises(RuntimeError):
            with self.connect() as connection:
                with connection.transaction():
                    append_call_updated(connection, self.org_a, call, "QUEUED", 0)
                    raise RuntimeError("abort synthetic transaction")
        with self.connect() as connection:
            rows = read_events(connection, Scope(self.org_a, "qa", "QA_ANALYST", frozenset()), 3)
            last = connection.execute("SELECT last_sequence FROM event_counters WHERE organisation_id=%s", (self.org_a,)).fetchone()[0]
        self.assertEqual(rows, [])
        self.assertEqual(last, 3)

    def test_retry_and_exhausted_lease_state_events_share_the_state_transaction(self):
        retry_call, retry_job_id = str(uuid4()), str(uuid4())
        failed_call, failed_job_id, failed_token = str(uuid4()), str(uuid4()), str(uuid4())
        with self.connect() as connection:
            self.insert_call(connection, self.org_a, retry_call, "agent-a", "team-a", "retry-ref", "ANALYSING")
            self.insert_call(connection, self.org_a, failed_call, "agent-a", "team-a", "failed-ref", "ANALYSING")
            base_sequence = connection.execute("SELECT last_sequence FROM event_counters WHERE organisation_id=%s", (self.org_a,)).fetchone()[0]
            connection.execute("INSERT INTO jobs(organisation_id,id,call_id,stage,state,attempts,lease_token,lease_until) VALUES (%s,%s,%s,'AUDIT','RUNNING',1,%s,now()+interval '1 minute')", (self.org_a, retry_job_id, retry_call, str(uuid4())))
            connection.execute("INSERT INTO jobs(organisation_id,id,call_id,stage,state,attempts,lease_token,lease_until) VALUES (%s,%s,%s,'AUDIT','RUNNING',5,%s,now()-interval '1 minute')", (self.org_a, failed_job_id, failed_call, failed_token))
            retry_token = connection.execute("SELECT lease_token FROM jobs WHERE organisation_id=%s AND id=%s", (self.org_a, retry_job_id)).fetchone()[0]
            self.assertTrue(retry_job(connection, retry_job_id, str(retry_token)))
            claim_job(connection, "events-test-worker", supported_stages=("AUDIT",))
            self.assertEqual(connection.execute("SELECT state FROM jobs WHERE organisation_id=%s AND id=%s", (self.org_a, failed_job_id)).fetchone()[0], "FAILED")
            retry_events = read_events(connection, Scope(self.org_a, "qa", "QA_ANALYST", frozenset()), base_sequence, limit=1)
            failed_event = read_events(connection, Scope(self.org_a, "qa", "QA_ANALYST", frozenset()), base_sequence + 1)
            states = connection.execute("SELECT id,processing_state FROM calls WHERE organisation_id=%s AND id=ANY(%s)", (self.org_a, [retry_call, failed_call])).fetchall()
        self.assertEqual({str(call_id): state for call_id, state in states}, {retry_call: "RETRY_WAIT", failed_call: "FAILED"})
        self.assertEqual([event["payload"]["processing_state"] for event in retry_events], ["RETRY_WAIT"])
        self.assertEqual([event["payload"]["processing_state"] for event in failed_event], ["FAILED"])

    def test_intake_and_successful_worker_state_commit_append_in_transaction(self):
        audio = io.BytesIO()
        with wave.open(audio, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16_000)
            writer.writeframes(b"\x00\x00" * 1600)
        scope = Scope(self.org_a, "agent-c", "AGENT", frozenset({"team-c"}))
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalPrivateStorage(directory)
            uploaded = accept_recording(scope, "synthetic-upload", audio.getvalue(), {}, "event-idem", storage=storage)
            duplicate = accept_recording(scope, "synthetic-upload", audio.getvalue(), {}, "event-idem", storage=storage)
        self.assertEqual(uploaded["id"], duplicate["id"])
        with self.connect() as connection:
            uploaded_events = read_events(connection, Scope(self.org_a, "qa", "QA_ANALYST", frozenset()), 3)
            self.assertEqual([event["payload"]["processing_state"] for event in uploaded_events], ["QUEUED"])
            self.insert_call(connection, self.org_a, str(uuid4()), "agent-a", "team-a", "worker-ref", "ANALYSING")
            worker_call = connection.execute("SELECT id FROM calls WHERE organisation_id=%s AND external_ref='worker-ref'", (self.org_a,)).fetchone()[0]
            job_id = str(uuid4())
            connection.execute(
                "INSERT INTO jobs(organisation_id,id,call_id,stage,state,input_revision) VALUES (%s,%s,%s,'AUDIT','QUEUED',0)",
                (self.org_a, job_id, worker_call),
            )
            job_id, token = str(uuid4()), str(uuid4())
            connection.execute("INSERT INTO jobs(organisation_id,id,call_id,stage,state,attempts,lease_token,lease_until) VALUES (%s,%s,%s,'AUDIT','RUNNING',1,%s,now()+interval '1 minute')", (self.org_a, job_id, worker_call, token))
            self.assertTrue(finish_job(connection, job_id, token, {"processing_state": "NEEDS_REVIEW"}))
            worker_events = read_events(connection, Scope(self.org_a, "qa", "QA_ANALYST", frozenset()), 4)
        self.assertEqual(len(worker_events), 1)
        self.assertEqual(worker_events[0]["payload"], {"processing_state": "NEEDS_REVIEW", "transcript_revision": 0})

        # POLICY commits findings without changing either call field. The refresh
        # event must still share the finding transaction so consumers fetch them.
        policy_call, policy_job = str(uuid4()), str(uuid4())
        with self.connect() as connection:
            self.insert_call(connection, self.org_a, policy_call, "agent-a", "team-a", "policy-ref", "ANALYSING")
            connection.execute("UPDATE calls SET transcript_revision=1 WHERE organisation_id=%s AND id=%s", (self.org_a, policy_call))
            connection.execute("INSERT INTO transcript_utterances(organisation_id,call_id,revision,id,segment_id,speaker_id,role,start_ms,end_ms,text_redacted,confidence,model_version,is_final) VALUES (%s,%s,1,'event-u1','event-seg1','channel-0','CUSTOMER',0,100,'Thanks.',0.9,'synthetic-asr',true)", (self.org_a, policy_call))
            connection.execute("INSERT INTO jobs(organisation_id,id,call_id,stage,input_revision,state,attempts,lease_token,lease_until) VALUES (%s,%s,%s,'POLICY',1,'RUNNING',1,%s,now()+interval '1 minute')", (self.org_a, policy_job, policy_call, str(uuid4())))
            lease = connection.execute("SELECT lease_token FROM jobs WHERE organisation_id=%s AND id=%s", (self.org_a, policy_job)).fetchone()[0]
            self.assertTrue(finish_job(connection, policy_job, str(lease), {"findings": [{"rule_id": "event_policy", "ruleset_version": "1", "ruleset_hash": "b" * 64, "policy_text_version": "synthetic_v1", "status": "SATISFIED", "severity": "LOW", "evidence_ids": ["event-u1"], "remediation": "Review this call."}]}))
            events = read_events(connection, Scope(self.org_a, "qa", "QA_ANALYST", frozenset()), 5)
            finding_count = connection.execute("SELECT count(*) FROM findings WHERE organisation_id=%s AND call_id=%s", (self.org_a, policy_call)).fetchone()[0]
        self.assertEqual(finding_count, 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"], {"processing_state": "ANALYSING", "transcript_revision": 1})


if __name__ == "__main__":
    unittest.main()
