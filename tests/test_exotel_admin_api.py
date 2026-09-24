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


class _Connection:
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
            self.state["integration"] = params
            return _Result()
        if query.startswith("INSERT INTO exotel_integration_events"):
            self.state["events"].append(params)
            return _Result()
        if "SELECT id,account_sid,username,is_active,created_at FROM exotel_integrations" in query:
            row = self.state.get("integration")
            if row is None or params[0] != row[0]:
                return _Result(many=[])
            return _Result(many=[(row[1], row[2], row[3], self.state["active"], self.state["created_at"])])
        if query.startswith("UPDATE exotel_integrations SET is_active=false"):
            row = self.state.get("integration")
            if row and params[:2] == (row[0], str(row[1])) and self.state["active"]:
                self.state["active"] = False
                return _Result((row[1],))
            return _Result()
        if query.startswith("SELECT 1 FROM exotel_integrations"):
            row = self.state.get("integration")
            exists = bool(row and params == (row[0], str(row[1])))
            return _Result((1,) if exists and ("is_active" not in query or self.state["active"]) else None)
        if "FROM identity_memberships" in query:
            return _Result(many=self.state["memberships"])
        if query.startswith("INSERT INTO exotel_agent_mappings"):
            self.state["mappings"].append(params)
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
        self.state = {
            "organisation_id": "org-a", "integration": None, "active": True,
            "created_at": self.now, "events": [], "memberships": [("team-a",)], "mappings": [],
        }
        self.connection = _Connection(self.state)

    def tearDown(self):
        self.client.close()

    def _headers(self, subject="admin"):
        token = jwt.encode({
            "sub": subject, "iss": self.settings.oidc_issuer, "aud": self.settings.oidc_audience,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        }, self.private_key, algorithm="RS256")
        return {"Authorization": f"Bearer {token}"}

    def _seed_integration(self):
        self.state["integration"] = ("org-a", self.integration_id, "acct", "generated-user", b"salt", b"verifier", "admin")

    def test_non_admin_is_denied_before_any_database_query(self):
        with patch("app.api.connect") as connect:
            response = self.client.post("/v1/exotel-integrations", headers=self._headers("qa"), json={"account_sid": "acct"})
        self.assertEqual(response.status_code, 403)
        connect.assert_not_called()

    def test_create_returns_secret_once_and_persists_only_verifier_with_audit_event(self):
        credentials = ("generated-user", "one-time-secret", b"salt", b"verifier")
        with patch("app.api.connect", return_value=self.connection), patch("app.api.integration_credentials", return_value=credentials):
            created = self.client.post("/v1/exotel-integrations", headers={**self._headers(), "X-Request-ID": "request-1"}, json={"account_sid": " acct "})
            listed = self.client.get("/v1/exotel-integrations", headers=self._headers())

        self.assertEqual(created.status_code, 201)
        data = created.json()
        self.assertEqual(data["password"], "one-time-secret")
        self.assertEqual(data["account_sid"], "acct")
        self.assertNotIn("password", listed.json()["integrations"][0])
        self.assertNotIn("one-time-secret", listed.text)
        stored = self.state["integration"]
        self.assertEqual(stored[4:6], (b"salt", b"verifier"))
        self.assertNotIn("one-time-secret", stored)
        self.assertEqual(self.state["events"][0][3:], ("CREATED", None, "request-1"))

    def test_disable_is_idempotent_and_cannot_cross_organisation(self):
        self._seed_integration()
        with patch("app.api.connect", return_value=self.connection):
            first = self.client.delete(f"/v1/exotel-integrations/{self.integration_id}", headers=self._headers())
            second = self.client.delete(f"/v1/exotel-integrations/{self.integration_id}", headers=self._headers())
        self.assertEqual((first.status_code, second.status_code), (204, 204))
        self.assertEqual([event[3] for event in self.state["events"]], ["DISABLED"])
        update = next((query, params) for query, params in self.connection.statements if query.startswith("UPDATE exotel_integrations"))
        self.assertEqual(update[1][:2], ("org-a", self.integration_id))

        self.state["active"] = True
        before_events = len(self.state["events"])
        with patch("app.api.connect", return_value=self.connection):
            cross_org = self.client.delete(f"/v1/exotel-integrations/{self.integration_id}", headers=self._headers("other-admin"))
        self.assertEqual(cross_org.status_code, 404)
        self.assertEqual(len(self.state["events"]), before_events)

    def test_agent_mapping_requires_one_active_same_org_membership_in_the_named_team(self):
        self._seed_integration()
        cases = (([], "missing membership"), ([("team-a",), ("team-b",)], "multiple memberships"), ([("team-b",)], "wrong team"))
        for memberships, label in cases:
            with self.subTest(label=label):
                self.state["memberships"] = memberships
                with patch("app.api.connect", return_value=self.connection):
                    response = self.client.put(
                        f"/v1/exotel-integrations/{self.integration_id}/agents/vendor-agent",
                        headers=self._headers(), json={"agent_id": "agent-a", "team_id": "team-a"},
                    )
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.state["mappings"], [])

        self.state["memberships"] = [("team-a",)]
        with patch("app.api.connect", return_value=self.connection):
            accepted = self.client.put(
                f"/v1/exotel-integrations/{self.integration_id}/agents/vendor-agent",
                headers=self._headers(), json={"agent_id": "agent-a", "team_id": "team-a"},
            )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json(), {"agent_ref": "vendor-agent", "agent_id": "agent-a", "team_id": "team-a"})
        membership_query = next((query, params) for query, params in self.connection.statements if "FROM identity_memberships" in query)
        self.assertEqual(membership_query[1], ("org-a", "agent-a"))
        self.assertIn("role='AGENT'", membership_query[0])
        self.assertIn("is_active", membership_query[0])
        self.assertEqual(self.state["mappings"][-1][0], "org-a")


if __name__ == "__main__":
    unittest.main()
