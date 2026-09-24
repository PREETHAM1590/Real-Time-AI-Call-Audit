"""Mocked API/query-contract checks; these do not verify PostgreSQL CRUD semantics."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api import create_app
from app.auth import Scope
from app.config import Settings


class _Result:
    def __init__(self, one=None, many=None):
        self.one = one
        self.many = many if many is not None else ([] if one is None else [one])

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


def _where_terms(query):
    if " WHERE " not in query:
        return []
    where = query.split(" WHERE ", 1)[1]
    for ending in (" ORDER BY ", " RETURNING "):
        where = where.split(ending, 1)[0]
    return [term.strip() for term in where.split(" AND ")]


def _matches(row, query, params, fields, *, active=None):
    values = iter(params)
    for term in _where_terms(query):
        if term.endswith("=%s"):
            column = term[:-3]
            if next(values) != row[fields[column]]:
                return False
        elif term == "is_active":
            if not active:
                return False
        elif term == "role='AGENT'":
            if row[fields["role"]] != "AGENT":
                return False
    return True


class _Connection:
    """Small SQL-shaped fake: predicates in SQL determine which rows match."""

    def __init__(self, state):
        self.state = state
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def transaction(self):
        return self

    def execute(self, query, params=()):
        self.statements.append((query, params))
        if query.startswith("INSERT INTO exotel_integrations"):
            org, integration_id, account_sid, username, salt, verifier, created_by = params
            self.state["integrations"].append((org, str(integration_id), account_sid, username, salt, verifier, created_by))
            self.state["active"][str(integration_id)] = True
            return _Result()
        if query.startswith("INSERT INTO exotel_integration_events"):
            self.state["events"].append(params)
            return _Result()
        if "SELECT id,account_sid,username,is_active,created_at FROM exotel_integrations" in query:
            rows = [row for row in self.state["integrations"] if _matches(row, query, params, {"organisation_id": 0, "id": 1})]
            return _Result(many=[(r[1], r[2], r[3], self.state["active"].get(r[1], True), self.state["created_at"]) for r in rows])
        if query.startswith("UPDATE exotel_integrations SET is_active=false"):
            rows = [row for row in self.state["integrations"] if _matches(row, query, params, {"organisation_id": 0, "id": 1}, active=self.state["active"].get(row[1], True))]
            if rows:
                self.state["active"][rows[0][1]] = False
                return _Result((rows[0][1],))
            return _Result()
        if query.startswith("SELECT 1 FROM exotel_integrations"):
            rows = [row for row in self.state["integrations"] if _matches(row, query, params, {"organisation_id": 0, "id": 1}, active=self.state["active"].get(row[1], True))]
            return _Result((1,) if rows else None)
        if "FROM identity_memberships" in query:
            rows = [row for row in self.state["memberships"] if _matches(row, query, params, {
                "organisation_id": 0, "user_id": 1, "role": 2, "team_id": 4,
            }, active=row[3])]
            return _Result(many=[(row[4],) for row in rows])
        if query.startswith("INSERT INTO exotel_agent_mappings"):
            self.state["mappings"].append((params[0], str(params[1]), params[2], params[3], params[4], params[5]))
            return _Result()
        if query.startswith("DELETE FROM exotel_agent_mappings"):
            fields = {"organisation_id": 0, "integration_id": 1, "agent_ref": 2}
            matches = [row for row in self.state["mappings"] if _matches(row, query, params, fields)]
            if matches:
                self.state["mappings"] = [row for row in self.state["mappings"] if row not in matches]
                return _Result((matches[0][2],))
            return _Result()
        raise AssertionError(f"unexpected SQL: {query}")


class ExotelAdminApiTests(unittest.TestCase):
    def setUp(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        public_key = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.settings = Settings(
            oidc_issuer="https://identity.test", oidc_audience="audit", oidc_public_key=public_key,
            csrf_secret="test-only-csrf-secret-at-least-32-bytes-long", allowed_origins=("https://audit.test",),
        )
        identities = {
            "admin": Scope("org-a", "admin", "ADMIN", frozenset()),
            "other-admin": Scope("org-b", "other-admin", "ADMIN", frozenset()),
            "qa": Scope("org-a", "qa", "QA_ANALYST", frozenset()),
        }
        self.client = TestClient(create_app(self.settings, identity_lookup=identities.get))
        self.now = datetime.now(timezone.utc)
        self.integration_id = str(uuid4())
        self.state = {"integrations": [], "active": {}, "created_at": self.now, "events": [], "memberships": [], "mappings": []}
        self.connection = _Connection(self.state)

    def tearDown(self):
        self.client.close()

    def _headers(self, subject="admin"):
        token = jwt.encode({
            "sub": subject, "iss": self.settings.oidc_issuer, "aud": self.settings.oidc_audience,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        }, self.private_key, algorithm="RS256")
        return {"Authorization": f"Bearer {token}"}

    def _seed_integration(self, org="org-a", integration_id=None):
        integration_id = integration_id or self.integration_id
        self.state["integrations"].append((org, integration_id, "acct", "generated-user", b"salt", b"verifier", "admin"))
        self.state["active"][integration_id] = True

    def test_non_admin_is_denied_before_any_database_query_on_all_admin_routes(self):
        with patch("app.api.connect") as connect:
            responses = (
                self.client.post("/v1/exotel-integrations", headers=self._headers("qa"), json={"account_sid": "acct"}),
                self.client.get("/v1/exotel-integrations", headers=self._headers("qa")),
                self.client.put(f"/v1/exotel-integrations/{self.integration_id}/agents/vendor-agent", headers=self._headers("qa"), json={"agent_id": "agent-a", "team_id": "team-a"}),
                self.client.delete(f"/v1/exotel-integrations/{self.integration_id}", headers=self._headers("qa")),
            )
        self.assertEqual([response.status_code for response in responses], [403, 403, 403, 403])
        connect.assert_not_called()

    def test_create_returns_secret_once_and_persists_only_verifier_with_audit_event(self):
        credentials = ("generated-user", "one-time-secret", b"salt", b"verifier")
        with patch("app.api.connect", return_value=self.connection), patch("app.api.integration_credentials", return_value=credentials):
            created = self.client.post("/v1/exotel-integrations", headers={**self._headers(), "X-Request-ID": "request-1"}, json={"account_sid": " acct "})
            listed = self.client.get("/v1/exotel-integrations", headers=self._headers())

        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["password"], "one-time-secret")
        self.assertEqual(created.json()["account_sid"], "acct")
        self.assertNotIn("password", listed.json()["integrations"][0])
        self.assertNotIn("one-time-secret", listed.text)
        self.assertEqual(self.state["integrations"][0][4:6], (b"salt", b"verifier"))
        self.assertNotIn("one-time-secret", self.state["integrations"][0])
        self.assertEqual(self.state["events"][0][3:], ("CREATED", None, "request-1"))

    def test_list_and_disable_sql_are_organisation_scoped_and_disable_is_idempotent(self):
        self._seed_integration("org-a")
        self._seed_integration("org-b", str(uuid4()))
        with patch("app.api.connect", return_value=self.connection):
            listed = self.client.get("/v1/exotel-integrations", headers=self._headers("other-admin"))
            first = self.client.delete(f"/v1/exotel-integrations/{self.integration_id}", headers=self._headers())
            second = self.client.delete(f"/v1/exotel-integrations/{self.integration_id}", headers=self._headers())
            cross_org = self.client.delete(f"/v1/exotel-integrations/{self.integration_id}", headers=self._headers("other-admin"))
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["integrations"]), 1)
        self.assertNotEqual(listed.json()["integrations"][0]["id"], self.integration_id)
        self.assertEqual((first.status_code, second.status_code, cross_org.status_code), (204, 204, 404))
        self.assertEqual([event[3] for event in self.state["events"]], ["DISABLED"])

        list_query = next(query for query, _ in self.connection.statements if "SELECT id,account_sid,username,is_active,created_at FROM exotel_integrations" in query)
        self.assertIn("WHERE organisation_id=%s", list_query)
        update_query = next(query for query, _ in self.connection.statements if query.startswith("UPDATE exotel_integrations"))
        self.assertIn("organisation_id=%s", update_query)
        self.assertIn("id=%s", update_query)
        exists_queries = [query for query, _ in self.connection.statements if query.startswith("SELECT 1 FROM exotel_integrations")]
        self.assertTrue(exists_queries)
        self.assertTrue(all("organisation_id=%s" in query for query in exists_queries))

    def test_agent_mapping_membership_query_filters_tenant_role_and_activity(self):
        self._seed_integration()
        def map_agent():
            return self.client.put(
                f"/v1/exotel-integrations/{self.integration_id}/agents/vendor-agent",
                headers=self._headers(), json={"agent_id": "agent-a", "team_id": "team-a"},
            )

        invalid_rows = (
            ([("org-b", "agent-a", "AGENT", True, "team-a")], "cross-organisation membership"),
            ([("org-a", "agent-a", "AGENT", False, "team-a")], "inactive membership"),
            ([("org-a", "agent-a", "TEAM_LEADER", True, "team-a")], "wrong role"),
            ([], "missing membership"),
            ([('org-a', 'agent-a', 'AGENT', True, 'team-a'), ('org-a', 'agent-a', 'AGENT', True, 'team-b')], "multiple memberships"),
            ([('org-a', 'agent-a', 'AGENT', True, 'team-b')], "wrong team"),
        )
        for rows, label in invalid_rows:
            with self.subTest(label=label):
                self.state["memberships"] = rows
                with patch("app.api.connect", return_value=self.connection):
                    response = map_agent()
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.state["mappings"], [])

        membership_query, membership_params = next((query, params) for query, params in self.connection.statements if "FROM identity_memberships" in query)
        self.assertIn("organisation_id=%s", membership_query)
        self.assertIn("user_id=%s", membership_query)
        self.assertIn("role='AGENT'", membership_query)
        self.assertIn("is_active", membership_query)
        self.assertEqual(membership_params, ("org-a", "agent-a"))

        self.state["memberships"] = [('org-a', 'agent-a', 'AGENT', True, 'team-a')]
        with patch("app.api.connect", return_value=self.connection):
            accepted = map_agent()
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json(), {"agent_ref": "vendor-agent", "agent_id": "agent-a", "team_id": "team-a"})
        self.assertEqual(self.state["mappings"][-1][0], "org-a")

    def test_agent_unmap_delete_sql_is_organisation_scoped(self):
        self._seed_integration()
        self.state["mappings"] = [("org-a", self.integration_id, "vendor-agent", "agent-a", "team-a", "admin")]
        with patch("app.api.connect", return_value=self.connection):
            response = self.client.delete(
                f"/v1/exotel-integrations/{self.integration_id}/agents/vendor-agent",
                headers=self._headers("other-admin"),
            )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(len(self.state["mappings"]), 1)
        delete_query = next(query for query, _ in self.connection.statements if query.startswith("DELETE FROM exotel_agent_mappings"))
        self.assertIn("organisation_id=%s", delete_query)
        self.assertIn("integration_id=%s", delete_query)
        self.assertIn("agent_ref=%s", delete_query)


if __name__ == "__main__":
    unittest.main()
