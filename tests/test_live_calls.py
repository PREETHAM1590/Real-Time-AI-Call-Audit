"""Scoped Exotel lifecycle and supervisor query contracts."""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app.auth import Scope
from app.ingest import IntakeError, accept_recording
from app.live_calls import (LiveCallsForbidden, activate_exotel_session, read_live_calls,
                            recording_intake_committed, recover_stale_exotel_sessions,
                            update_exotel_session)


class _Result:
    def __init__(self, one=None, many=None, rowcount=0):
        self.one = one
        self.many = [] if many is None else many
        self.rowcount = rowcount

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _Connection:
    def __init__(self, *, rows=(), generation=5, mapping=("agent-a", "team-a"), memberships=(("team-a",),), committed=False):
        self.rows = list(rows)
        self.generation = generation
        self.mapping = mapping
        self.memberships = list(memberships)
        self.committed = committed
        self.queries = []

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "JOIN exotel_agent_mappings" in query:
            return _Result(self.mapping)
        if "FROM identity_memberships" in query:
            return _Result(many=self.memberships)
        if "INSERT INTO exotel_sessions" in query:
            return _Result((self.generation,))
        if "UPDATE exotel_sessions" in query:
            if "SET state='INCOMPLETE'" in query:
                return _Result(rowcount=1)
            return _Result((params[-1],))
        if "SELECT 1 FROM calls" in query:
            return _Result((1,) if self.committed else None)
        if "SELECT call_key" in query:
            return _Result(many=[row for row in self.rows if row[3] != "UNKNOWN"])
        raise AssertionError(f"Unexpected query: {query}")


class _IntakeConnection:
    def __init__(self, *, allow_ended=True, commit_error=False):
        self.call_id = uuid4()
        self.allow_ended = allow_ended
        self.commit_error = commit_error
        self.in_transaction = False
        self.ended_was_in_transaction = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def transaction(self):
        connection = self

        class _Transaction:
            def __enter__(self):
                connection.in_transaction = True
                return connection

            def __exit__(self, *args):
                connection.in_transaction = False
                if connection.commit_error and args[0] is None:
                    raise RuntimeError("ambiguous commit result")
                return False

        return _Transaction()

    def execute(self, query, params=()):
        if "SELECT s.generation,s.state" in query:
            return _Result((7, "DRAINING"))
        if "FROM identity_memberships" in query:
            return _Result(many=[("team-a",)])
        if "INSERT INTO calls(" in query:
            return _Result((self.call_id,))
        if "INSERT INTO event_counters" in query:
            return _Result((1,))
        if "SELECT oldest_sequence" in query:
            return _Result((1,))
        if "SELECT sequence FROM events" in query or "SELECT 1 FROM calls c JOIN exotel_sessions" in query:
            return _Result()
        if "UPDATE exotel_sessions SET state='ENDED'" in query:
            self.ended_was_in_transaction = self.in_transaction
            return _Result((7,) if self.allow_ended else None)
        return _Result()


class _Storage:
    def __init__(self):
        self.deleted = []

    def put(self, _audio):
        return "synthetic-object"

    def delete(self, key):
        self.deleted.append(key)


class LiveSessionLifecycleTests(unittest.TestCase):
    def test_activation_revalidates_mapping_before_marking_live(self):
        connection = _Connection()
        generation = activate_exotel_session(connection, "org-a", "integration-a", "a" * 64,
                                             "external-agent", "agent-a", "team-a")
        self.assertEqual(generation, 5)
        insert = next(query for query, _ in connection.queries if "INSERT INTO exotel_sessions" in query)
        self.assertIn("'LIVE'", insert)
        self.assertIn("FOR UPDATE OF i,m", connection.queries[0][0])

        stale_mapping = _Connection(mapping=("agent-b", "team-a"))
        with self.assertRaises(ValueError):
            activate_exotel_session(stale_mapping, "org-a", "integration-a", "a" * 64,
                                    "external-agent", "agent-a", "team-a")
        self.assertFalse(any("INSERT INTO exotel_sessions" in query for query, _ in stale_mapping.queries))

    def test_only_current_live_generation_can_transition(self):
        connection = _Connection()
        self.assertTrue(update_exotel_session(connection, "org-a", "integration-a", "a" * 64, 5, "DRAINING"))
        query, params = connection.queries[-1]
        self.assertIn("generation=%s", query)
        self.assertIn("state IN ('LIVE')", query)
        self.assertEqual(params[0], "DRAINING")
        with self.assertRaises(ValueError):
            update_exotel_session(connection, "org-a", "integration-a", "a" * 64, 5, "FAILED")

    def test_nominal_transition_matrix_and_activity_guard(self):
        connection = _Connection()
        for state, activity in (("LIVE", True), ("DRAINING", False), ("ENDED", False)):
            self.assertTrue(update_exotel_session(connection, "org-a", "integration-a", "a" * 64, 5, state, activity=activity))
        self.assertIn("AND state IN ('LIVE')", connection.queries[1][0])
        self.assertIn("AND state IN ('DRAINING')", connection.queries[2][0])
        self.assertIn("last_activity_at=now()", connection.queries[0][0])
        with self.assertRaises(ValueError):
            update_exotel_session(connection, "org-a", "integration-a", "a" * 64, 5, "LIVE")
        with self.assertRaises(ValueError):
            update_exotel_session(connection, "org-a", "integration-a", "a" * 64, 5, "DRAINING", activity=True)

    def test_stale_recovery_is_bounded_for_live_and_draining(self):
        connection = _Connection()
        self.assertEqual(recover_stale_exotel_sessions(connection, "org-a"), 1)
        query = connection.queries[0][0]
        self.assertIn("last_activity_at<now()-interval '90 seconds'", query)
        self.assertIn("draining_at<now()-interval '2 hours 1 minute'", query)

    def test_migration_hides_legacy_sessions_as_unknown_and_backfills_times(self):
        migration = (Path(__file__).resolve().parent.parent / "migrations" / "018_exotel_session_lifecycle.sql").read_text(encoding="utf-8")
        self.assertIn("DEFAULT 'UNKNOWN'", migration)
        self.assertIn("SET updated_at=started_at,last_activity_at=started_at", migration)
        now = datetime.now(timezone.utc)
        legacy = ("b" * 64, None, None, "UNKNOWN", now, None, None, None, now, now)
        self.assertEqual(read_live_calls(_Connection(rows=[legacy]), Scope("org-a", "qa-a", "QA_ANALYST", frozenset())), [])

    def test_reconcile_confirms_only_committed_ended_session(self):
        self.assertTrue(recording_intake_committed(_Connection(committed=True), "org-a", "integration-a", "a" * 64, 5, "sha256:ref"))
        self.assertFalse(recording_intake_committed(_Connection(committed=False), "org-a", "integration-a", "a" * 64, 5, "sha256:ref"))

    def test_accept_recording_commits_ended_transition_inside_intake_transaction(self):
        connection = _IntakeConnection()
        storage = _Storage()
        scope = Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"}))
        with patch("app.ingest.connect", return_value=connection), patch("app.ingest.inspect_audio", return_value=("wav", 8000, 1, 100)):
            result = accept_recording(scope, "sha256:ref", b"synthetic", {}, "idem",
                                      storage=storage, generation_fence=("integration-a", "a" * 64, 7, "external-agent", "agent-a", "team-a"))
        self.assertEqual(result["processing_state"], "QUEUED")
        self.assertTrue(connection.ended_was_in_transaction)
        self.assertEqual(storage.deleted, [])

    def test_failed_ended_transition_rolls_back_and_does_not_claim_acceptance(self):
        connection = _IntakeConnection(allow_ended=False)
        storage = _Storage()
        scope = Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"}))
        with patch("app.ingest.connect", return_value=connection), patch("app.ingest.inspect_audio", return_value=("wav", 8000, 1, 100)):
            with self.assertRaisesRegex(IntakeError, "finalized"):
                accept_recording(scope, "sha256:ref", b"synthetic", {}, "idem",
                                 storage=storage, generation_fence=("integration-a", "a" * 64, 7, "external-agent", "agent-a", "team-a"))
        self.assertTrue(connection.ended_was_in_transaction)
        self.assertEqual(storage.deleted, ["synthetic-object"])

    def test_ambiguous_commit_retains_object_for_orphan_reconciliation(self):
        connection = _IntakeConnection(commit_error=True)
        storage = _Storage()
        scope = Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"}))
        with patch("app.ingest.connect", return_value=connection), patch("app.ingest.inspect_audio", return_value=("wav", 8000, 1, 100)):
            with self.assertRaisesRegex(RuntimeError, "ambiguous commit"):
                accept_recording(scope, "sha256:ref", b"synthetic", {}, "idem", storage=storage)
        self.assertEqual(storage.deleted, [])


class LiveCallsScopeTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.row = ("a" * 64, "agent-a", "team-a", "LIVE", now, None, None, None,
                    now, now - timedelta(seconds=60))

    def test_team_leader_query_is_tenant_and_authorized_team_scoped(self):
        connection = _Connection(rows=[self.row])
        result = read_live_calls(connection, Scope("org-a", "leader-a", "TEAM_LEADER", frozenset({"team-a", "team-b"})))
        query, params = connection.queries[-1]
        self.assertIn("organisation_id=%s::uuid", query)
        self.assertIn("team_id=ANY(%s)", query)
        self.assertEqual(params, ("org-a", ["team-a", "team-b"]))
        self.assertTrue(any("state<>'UNKNOWN'" in query for query, _ in connection.queries))
        self.assertTrue(result[0]["stale"])
        self.assertEqual(result[0]["call_key"], "a" * 64)

    def test_agent_is_self_scoped_and_other_tenant_never_queries(self):
        connection = _Connection(rows=[self.row])
        read_live_calls(connection, Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"})))
        query, params = connection.queries[-1]
        self.assertIn("agent_id=%s", query)
        self.assertNotIn("team_id=ANY", query)
        self.assertEqual(params, ("org-a", "agent-a"))

        no_teams = _Connection(rows=[self.row])
        self.assertEqual(read_live_calls(no_teams, Scope("org-b", "leader-b", "TEAM_LEADER", frozenset())), [])
        self.assertEqual(no_teams.queries, [])

    def test_reviewer_has_tenant_scope_and_unknown_role_is_denied(self):
        connection = _Connection(rows=[self.row])
        read_live_calls(connection, Scope("org-a", "qa-a", "QA_ANALYST", frozenset()))
        query, params = connection.queries[-1]
        self.assertIn("organisation_id=%s::uuid", query)
        self.assertNotIn("team_id=ANY", query)
        self.assertEqual(params, ("org-a",))
        with self.assertRaises(LiveCallsForbidden):
            read_live_calls(_Connection(), Scope("org-a", "service-a", "SERVICE", frozenset()))


if __name__ == "__main__":
    unittest.main()
