import asyncio
from datetime import datetime, timedelta, timezone
import unittest

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import ValidationError
from unittest.mock import patch, MagicMock

from app.auth import Scope, can_access
from app.api import create_app
from app.config import Settings
from app.contracts import PersistedUtterance, Utterance


class ContractTests(unittest.TestCase):
    def test_scope_and_invalid_time(self):
        agent = Scope("org-a", "agent-a", "AGENT", frozenset())
        self.assertTrue(can_access(agent, "org-a", "agent-a", "team-a"))
        self.assertFalse(can_access(agent, "org-b", "agent-a", "team-a"))
        self.assertFalse(can_access(agent, "org-a", "agent-b", "team-a"))
        with self.assertRaises(ValidationError):
            Utterance(
                id="u1",
                role="AGENT",
                start_ms=20,
                end_ms=10,
                text_redacted="Hello",
            )

    def test_utterance_rejects_untrusted_fields_and_invalid_values(self):
        with self.assertRaises(ValidationError):
            Utterance(
                id="u1",
                role="SPEAKER_0",
                start_ms=0,
                end_ms=10,
                text_redacted="Hello",
            )
        with self.assertRaises(ValidationError):
            Utterance(
                id="u1",
                role="AGENT",
                start_ms=-1,
                end_ms=10,
                text_redacted="Hello",
            )
        with self.assertRaises(ValidationError):
            Utterance(
                id="u1",
                role="AGENT",
                start_ms=0,
                end_ms=10,
                text_redacted="Hello",
                raw_text="must not persist",
            )
        with self.assertRaises(ValidationError):
            Utterance(
                id="u1",
                role="AGENT",
                start_ms=0,
                end_ms=10,
                text_redacted="x" * 10_001,
            )

    def test_persisted_utterance_carries_scope_and_local_model_provenance(self):
        utterance = PersistedUtterance(
            id="u1",
            role="AGENT",
            start_ms=0,
            end_ms=10,
            text_redacted="Hello",
            organisation_id="org-a",
            call_id="call-a",
            revision=1,
            segment_id="channel-0:segment-1",
            speaker_id="channel-0",
            model_version="local-whisper-v1",
            confidence=0.97,
        )
        self.assertEqual(utterance.organisation_id, "org-a")
        self.assertEqual(utterance.model_version, "local-whisper-v1")


class SettingsTests(unittest.TestCase):
    def test_allowed_origins_require_normalized_absolute_origins(self):
        settings = Settings(
            oidc_issuer="https://identity.example.test",
            oidc_audience="call-audit",
            oidc_public_key="test-key",
            allowed_origins=("https://audit.example.test",),
        )
        self.assertEqual(settings.allowed_origins, ("https://audit.example.test",))

        for origin in (
            "*",
            "audit.example.test",
            "https://audit.example.test/path",
            "https://*.example.test",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValidationError):
                Settings(
                    oidc_issuer="https://identity.example.test",
                    oidc_audience="call-audit",
                    oidc_public_key="test-key",
                    allowed_origins=(origin,),
                )


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_key = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        self.settings = Settings(
            oidc_issuer="https://identity.example.test",
            oidc_audience="call-audit",
            oidc_public_key=public_key,
            allowed_origins=("https://audit.example.test",),
        )
        identities = {"user-a": Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"}))}
        self.client = TestClient(
            create_app(
                self.settings,
                identity_lookup=identities.get,
            )
        )

    def tearDown(self):
        self.client.close()

    def token(self, subject="user-a", key=None):
        return jwt.encode(
            {
                "sub": subject,
                "iss": self.settings.oidc_issuer,
                "aud": self.settings.oidc_audience,
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            key or self.private_key,
            algorithm="RS256",
        )

    def test_health_and_call_routes_require_valid_scoped_identity(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/v1/calls/call-a").status_code, 401)

        forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = self.token(
            key=forged_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        self.assertEqual(
            self.client.get(
                "/v1/calls/call-a", headers={"Authorization": f"Bearer {forged}"}
            ).status_code,
            401,
        )

        headers = {"Authorization": f"Bearer {self.token()}"}
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.side_effect = [
            ("call-a", "org-a", "agent-a", "team-a", "QUEUED"),
            None,
        ]
        with patch("app.api.connect", return_value=connection):
            self.assertEqual(self.client.get("/v1/calls/call-a", headers=headers).json(), {"id": "call-a", "processing_state": "QUEUED"})
            self.assertEqual(self.client.get("/v1/calls/call-b", headers=headers).status_code, 404)
        params = connection.__enter__.return_value.execute.call_args_list
        self.assertEqual(params[0].args[1][0], "org-a")

    def test_upload_uses_server_identity_and_rejects_unmapped_assignment(self):
        headers = {"Authorization": f"Bearer {self.token()}"}
        with patch("app.api.accept_recording", return_value={"id": "call-a", "processing_state": "QUEUED"}) as accept:
            response = self.client.post(
                "/v1/calls",
                headers={**headers, "Idempotency-Key": "request-1"},
                files={"audio": ("call.wav", b"synthetic", "audio/wav")},
                data={"external_ref": "external-1", "agent_id": "forged", "team_id": "other-team"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(accept.call_args.args[3], {"language": "und"})
        self.assertEqual(accept.call_args.args[0], Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"})))

        app = create_app(self.settings, identity_lookup=lambda _: Scope("org-a", "qa-a", "QA_ANALYST", frozenset()))
        staff = TestClient(app)
        response = staff.post(
            "/v1/calls",
            headers={"Authorization": f"Bearer {self.token()}", "Idempotency-Key": "request-2"},
            files={"audio": ("call.wav", b"synthetic", "audio/wav")},
            data={"external_ref": "external-2", "agent_id": "forged", "team_id": "other-team"},
        )
        self.assertEqual(response.status_code, 403)
        staff.close()

    def test_forbidden_origin_is_rejected(self):
        response = self.client.options(
            "/v1/calls/call-a",
            headers={
                "Origin": "https://untrusted.example.test",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_upload_preflight_allows_idempotency_header(self):
        response = self.client.options(
            "/v1/calls",
            headers={
                "Origin": "https://audit.example.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,idempotency-key",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("idempotency-key", response.headers["access-control-allow-headers"].lower())

    def test_upload_runs_blocking_intake_off_the_event_loop(self):
        def accept_off_loop(*_args):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return {"id": "call-a", "processing_state": "QUEUED"}
            self.fail("blocking intake ran on the event loop")

        with patch("app.api.accept_recording", side_effect=accept_off_loop):
            response = self.client.post(
                "/v1/calls",
                headers={"Authorization": f"Bearer {self.token()}", "Idempotency-Key": "request-off-loop"},
                files={"audio": ("call.wav", b"synthetic", "audio/wav")},
                data={"external_ref": "external-off-loop"},
            )
        self.assertEqual(response.status_code, 202)
