"""Scoped Exotel lifecycle and supervisor query contracts."""

import unittest
from datetime import datetime, timedelta, timezone

from app.auth import Scope
from app.live_calls import LiveCallsForbidden, activate_exotel_session, read_live_calls, update_exotel_session


class _Result:
    def __init__(self, one=None, many=None):
        self.one = one
        self.many = [] if many is None else many

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _Connection:
    def __init__(self, *, rows=(), generation=5, mapping=("agent-a", "team-a"), memberships=(("team-a",),)):
        self.rows = list(rows)
        self.generation = generation
        self.mapping = mapping
        self.memberships = list(memberships)
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
            return _Result((params[-1],))
        if "SELECT call_key" in query:
            return _Result(many=self.rows)
        raise AssertionError(f"Unexpected query: {query}")


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
        self.assertIn("state IN ('LIVE','DRAINING')", query)
        self.assertEqual(params[0], "DRAINING")
        with self.assertRaises(ValueError):
            update_exotel_session(connection, "org-a", "integration-a", "a" * 64, 5, "FAILED")


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
