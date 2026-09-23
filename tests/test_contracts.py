import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from urllib.parse import urlparse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import ValidationError
from unittest.mock import patch, MagicMock
from uuid import uuid4

from app.auth import Scope, can_access
from app.auth import IdentityStoreError, resolve_identity_from_db
from app.api import MULTIPART_OVERHEAD_BYTES, UploadBodyLimitMiddleware, create_app
from app.config import Settings
from app.contracts import PersistedUtterance, Utterance
from app.db import connect
from app.ingest import MAX_AUDIO_BYTES
from app.migrate import migrate
from app.reviews import ReviewNotFound

CSRF_SECRET = "test-only-csrf-secret-at-least-32-bytes-long"


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
            csrf_secret=CSRF_SECRET,
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
                    csrf_secret=CSRF_SECRET,
                    allowed_origins=(origin,),
                )

    def test_csrf_secret_must_not_be_template_placeholder(self):
        with self.assertRaises(ValidationError):
            Settings(
                oidc_issuer="https://identity.example.test",
                oidc_audience="call-audit",
                oidc_public_key="test-key",
                csrf_secret="replace-with-a-random-secret-at-least-32-bytes",
                allowed_origins=("https://audit.example.test",),
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
            csrf_secret=CSRF_SECRET,
            allowed_origins=("https://audit.example.test",),
        )
        identities = {
            "user-a": Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"})),
            "admin-user": Scope("org-a", "admin-user", "ADMIN", frozenset()),
            "qa-user": Scope("org-a", "qa-user", "QA_ANALYST", frozenset()),
        }
        self.identity_lookup = MagicMock(side_effect=identities.get)
        self.client = TestClient(
            create_app(
                self.settings,
                identity_lookup=self.identity_lookup,
            )
        )

    def tearDown(self):
        self.client.close()

    def token(self, subject="user-a", key=None, claims=None):
        payload = {
            "sub": subject,
            "iss": self.settings.oidc_issuer,
            "aud": self.settings.oidc_audience,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        payload.update(claims or {})
        return jwt.encode(
            payload,
            key or self.private_key,
            algorithm="RS256",
        )

    def test_health_and_call_routes_require_valid_scoped_identity(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/v1/calls/call-a").status_code, 401)

    def test_call_deletion_requires_admin_and_returns_tombstone_status(self):
        qa_headers = {"Authorization": f"Bearer {self.token(subject='qa-user')}"}
        with patch("app.api.connect") as connect:
            denied = self.client.delete(f"/v1/calls/{uuid4()}", headers=qa_headers)
        self.assertEqual(denied.status_code, 403)
        connect.assert_not_called()

        admin_headers = {"Authorization": f"Bearer {self.token(subject='admin-user')}"}
        call_id = uuid4()
        with patch("app.api.connect"), patch("app.api.request_call_deletion", return_value={"status": "TOMBSTONED", "cancelled_jobs": 2}) as request_delete:
            accepted = self.client.delete(f"/v1/calls/{call_id}", headers=admin_headers)
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(accepted.json(), {"call_id": str(call_id), "status": "TOMBSTONED", "cancelled_jobs": 2})
        self.assertEqual(request_delete.call_args.args[1:], ("org-a", str(call_id), "admin-user"))

        with patch("app.api.connect"), patch("app.api.request_call_deletion", return_value={"status": "HELD"}):
            held = self.client.delete(f"/v1/calls/{call_id}", headers=admin_headers)
        self.assertEqual(held.status_code, 409)

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

        for invalid_claims in (
            {"exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
            {"iss": "https://wrong-issuer.example.test"},
            {"aud": "wrong-audience"},
        ):
            with self.subTest(claims=invalid_claims):
                invalid = self.token(claims=invalid_claims)
                self.assertEqual(self.client.get("/v1/calls/call-a", headers={"Authorization": f"Bearer {invalid}"}).status_code, 401)

        headers = {"Authorization": f"Bearer {self.token()}"}
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.side_effect = [
            ("call-a", "org-a", "agent-a", "team-a", "QUEUED"),
            None,
        ]
        with patch("app.api.connect", return_value=connection):
            with patch("app.api.call_detail", side_effect=[{"call": {"id": "call-a", "processing_state": "QUEUED"}}, ReviewNotFound()]):
                self.assertEqual(self.client.get("/v1/calls/call-a", headers=headers).json(), {"call": {"id": "call-a", "processing_state": "QUEUED"}})
                self.assertEqual(self.client.get("/v1/calls/call-b", headers=headers).status_code, 404)
        invalid_uuid_connection = MagicMock()
        invalid_uuid_connection.__enter__.return_value = invalid_uuid_connection
        with patch("app.api.connect", return_value=invalid_uuid_connection):
            self.assertEqual(self.client.get("/v1/calls/not-a-uuid", headers=headers).status_code, 404)
        invalid_uuid_connection.execute.assert_not_called()

    def test_default_identity_store_failure_returns_service_unavailable(self):
        client = TestClient(create_app(self.settings))
        try:
            with patch("app.auth.resolve_identity_from_db", side_effect=IdentityStoreError("unavailable")):
                response = client.get("/v1/csrf", headers={"Authorization": f"Bearer {self.token()}"})
            self.assertEqual(response.status_code, 503)
        finally:
            client.close()

    def test_upload_uses_server_identity_and_rejects_unmapped_assignment(self):
        headers = {"Authorization": f"Bearer {self.token()}"}
        self.identity_lookup.reset_mock()
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
        self.identity_lookup.assert_called_once_with("user-a")

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
                "Access-Control-Request-Headers": "authorization,content-type,idempotency-key,x-csrf-token,x-organisation-id",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("idempotency-key", response.headers["access-control-allow-headers"].lower())
        self.assertIn("x-csrf-token", response.headers["access-control-allow-headers"].lower())
        self.assertIn("x-organisation-id", response.headers["access-control-allow-headers"].lower())

    def test_review_api_enforces_analyst_role_and_maps_stale_conflict(self):
        from app.reviews import ReviewConflict

        self.identity_lookup.side_effect = lambda subject: {
            "qa-user": Scope("org-a", "qa-user", "QA_ANALYST", frozenset()),
            "agent-user": Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"})),
        }.get(subject)
        qa_headers = {"Authorization": f"Bearer {self.token(subject='qa-user')}", "X-Request-ID": "review-request-1"}
        body = {"action": "ACCEPT", "base_review_version": 0, "scores": {}, "reason": "Synthetic review confirmation"}
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch("app.api.connect", return_value=connection), patch("app.api.append_review", return_value={"version": 1, "action": "ACCEPT"}) as append:
            saved = self.client.post("/v1/audits/00000000-0000-4000-8000-000000000001/reviews", headers=qa_headers, json=body)
            self.assertEqual(saved.status_code, 201)
            self.assertEqual(append.call_args.args[1], Scope("org-a", "qa-user", "QA_ANALYST", frozenset()))
            self.assertEqual(append.call_args.kwargs["request_id"], "review-request-1")

        agent_headers = {"Authorization": f"Bearer {self.token(subject='agent-user')}"}
        denied = self.client.post("/v1/audits/00000000-0000-4000-8000-000000000001/reviews", headers=agent_headers, content=b"not-json")
        self.assertEqual(denied.status_code, 403)
        self.identity_lookup.side_effect = lambda subject: Scope("org-a", "qa-user", "QA_ANALYST", frozenset()) if subject == "qa-user" else None
        with patch("app.api.connect", return_value=connection), patch("app.api.append_review", side_effect=ReviewConflict("stale")):
            stale = self.client.post("/v1/audits/00000000-0000-4000-8000-000000000001/reviews", headers=qa_headers, json={**body, "action": "OVERRIDE", "scores": {"clarity": 2}})
        self.assertEqual(stale.status_code, 409)

    def test_review_body_is_bounded_before_json_parse_and_queue_requires_qa(self):
        qa = Scope("org-a", "qa-user", "QA_ANALYST", frozenset())
        self.identity_lookup.side_effect = lambda subject: qa if subject == "qa-user" else Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"}))
        oversized = self.client.post(
            "/v1/audits/00000000-0000-4000-8000-000000000001/reviews",
            headers={"Authorization": f"Bearer {self.token(subject='qa-user')}"},
            content=b"{" + b" " * 32768,
        )
        self.assertEqual(oversized.status_code, 413)
        with patch("app.api.review_queue", return_value=[]):
            self.assertEqual(self.client.get("/v1/reviews/queue", headers={"Authorization": f"Bearer {self.token(subject='qa-user')}"}).json(), {"items": []})
        with patch("app.api.connect"):
            denied = self.client.get("/v1/reviews/queue", headers={"Authorization": f"Bearer {self.token(subject='agent-user')}"})
        self.assertEqual(denied.status_code, 403)

    def test_audio_access_is_logged_streamed_by_range_and_rechecks_permission(self):
        from pathlib import Path

        organisation_id = "00000000-0000-4000-8000-000000000010"
        call_id = "00000000-0000-4000-8000-000000000011"
        audio_id = "00000000-0000-4000-8000-000000000012"
        role = {"current": "QA_ANALYST"}
        self.identity_lookup.side_effect = lambda subject: (
            Scope(organisation_id, "qa-user", role["current"], frozenset({"team-a"}))
            if subject == "qa-user" else Scope(organisation_id, "agent-a", "AGENT", frozenset({"team-a"})) if subject == "agent-user" else None
        )
        qa_headers = {"Authorization": f"Bearer {self.token(subject='qa-user')}"}
        agent_headers = {"Authorization": f"Bearer {self.token(subject='agent-user')}"}
        denied = self.client.post(f"/v1/calls/{call_id}/audio-access", headers=agent_headers)
        self.assertEqual(denied.status_code, 403)

        grant_connection = MagicMock()
        grant_connection.__enter__.return_value = grant_connection
        grant_connection.execute.return_value.fetchone.return_value = (audio_id, "fixture.audio", "wav", "qa-user", "team-a")
        with patch("app.api.connect", return_value=grant_connection):
            grant = self.client.post(f"/v1/calls/{call_id}/audio-access", headers=qa_headers)
        self.assertEqual(grant.status_code, 200)
        self.assertEqual(grant.json()["expires_in_seconds"], 60)
        event_calls = [call.args[0] for call in grant_connection.execute.call_args_list]
        self.assertTrue(any("AUDIO_ACCESS_GRANTED" in query for query in event_calls))

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "fixture.audio").write_bytes(b"0123456789")
            stream_connection = MagicMock()
            stream_connection.__enter__.return_value = stream_connection
            stream_connection.execute.return_value.fetchone.return_value = ("fixture.audio", "wav", "qa-user", "team-a", None)
            with patch("app.api.connect", return_value=stream_connection), patch.dict(os.environ, {"AUDIO_STORAGE_PATH": directory}):
                streamed = self.client.get(grant.json()["url"], headers={**qa_headers, "Range": "bytes=2-5"})
            self.assertEqual(streamed.status_code, 206)
            self.assertEqual(streamed.content, b"2345")
            self.assertEqual(streamed.headers["content-range"], "bytes 2-5/10")
            with patch("app.api.connect", return_value=stream_connection), patch.dict(os.environ, {"AUDIO_STORAGE_PATH": directory}):
                zero_suffix = self.client.get(grant.json()["url"], headers={**qa_headers, "Range": "bytes=-0"})
                huge_range = self.client.get(grant.json()["url"], headers={**qa_headers, "Range": "bytes=" + "9" * 5000 + "-"})
            self.assertEqual(zero_suffix.status_code, 416)
            self.assertEqual(huge_range.status_code, 416)

            Path(directory, "fixture.audio").write_bytes(b"")
            empty_connection = MagicMock()
            empty_connection.__enter__.return_value = empty_connection
            empty_connection.execute.return_value.fetchone.return_value = ("fixture.audio", "wav", "qa-user", "team-a", None)
            with patch("app.api.connect", return_value=empty_connection), patch.dict(os.environ, {"AUDIO_STORAGE_PATH": directory}):
                empty = self.client.get(grant.json()["url"], headers=qa_headers)
            self.assertEqual(empty.status_code, 404)

            role["current"] = "AGENT"
            with patch("app.api.connect") as connect:
                revoked = self.client.get(grant.json()["url"], headers=qa_headers)
            self.assertEqual(revoked.status_code, 403)
            connect.assert_not_called()

            role["current"] = "QA_ANALYST"
            tombstoned_connection = MagicMock()
            tombstoned_connection.__enter__.return_value = tombstoned_connection
            tombstoned_connection.execute.return_value.fetchone.return_value = ("fixture.audio", "wav", "qa-user", "team-a", datetime.now(timezone.utc))
            with patch("app.api.connect", return_value=tombstoned_connection), patch.dict(os.environ, {"AUDIO_STORAGE_PATH": directory}):
                unavailable = self.client.get(grant.json()["url"], headers=qa_headers)
            self.assertEqual(unavailable.status_code, 404)

    def test_scoped_report_and_compliance_export_api_contracts(self):
        from app.reports import ReportForbidden

        identities = {
            "agent-report": Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"})),
            "leader-report": Scope("org-a", "leader-a", "TEAM_LEADER", frozenset({"team-a"})),
            "compliance-report": Scope("org-a", "compliance-a", "COMPLIANCE_OFFICER", frozenset()),
            "qa-report": Scope("org-a", "qa-a", "QA_ANALYST", frozenset()),
        }
        self.identity_lookup.side_effect = identities.get
        agent_headers = {"Authorization": f"Bearer {self.token(subject='agent-report')}"}
        with patch("app.api.connect"), patch("app.api.own_scores", return_value=[{"call_id": "synthetic-call"}]) as own:
            self.assertEqual(self.client.get("/v1/me/scores", headers=agent_headers).json(), {"items": [{"call_id": "synthetic-call"}]})
            self.assertEqual(own.call_args.args[1], identities["agent-report"])
            self.assertTrue(callable(own.call_args.kwargs["redact"]))

        qa_headers = {"Authorization": f"Bearer {self.token(subject='qa-report')}"}
        with patch("app.api.connect") as connect:
            forbidden_own = self.client.get("/v1/me/scores", headers=qa_headers)
        self.assertEqual(forbidden_own.status_code, 403)
        connect.assert_not_called()

        start, end = "2026-09-01T00:00:00Z", "2026-09-30T00:00:00Z"
        leader_headers = {"Authorization": f"Bearer {self.token(subject='leader-report')}"}
        with patch("app.api.connect"), patch("app.api.team_report", return_value={"cohorts": [{"team_id": "team-a"}]}) as report:
            response = self.client.get(f"/v1/reports/team?start={start}&end={end}", headers=leader_headers)
        self.assertEqual(response.json(), {"cohorts": [{"team_id": "team-a"}]})
        self.assertEqual(report.call_args.args[1], identities["leader-report"])
        with patch("app.api.connect") as connect:
            denied_team = self.client.get(f"/v1/reports/team?start={start}&end={end}", headers=agent_headers)
        self.assertEqual(denied_team.status_code, 403)
        connect.assert_not_called()

        compliance_headers = {"Authorization": f"Bearer {self.token(subject='compliance-report')}", "X-Request-ID": "export-req"}
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch("app.api.connect", return_value=connection), patch("app.api.export_findings", return_value=(b"rule_id\r\n'=1+1\r\n", 1)) as export:
            exported = self.client.get(f"/v1/exports/findings?start={start}&end={end}&limit=20", headers=compliance_headers)
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.content, b"rule_id\r\n'=1+1\r\n")
        self.assertTrue(exported.headers["content-disposition"].startswith("attachment; filename=\"findings-"))
        self.assertEqual(exported.headers["x-export-row-count"], "1")
        self.assertIn("FINDINGS_EXPORTED", " ".join(call.args[0] for call in connection.execute.call_args_list))
        self.assertEqual(export.call_args.kwargs["limit"], 20)
        with patch("app.api.connect") as connect:
            denied_export = self.client.get(f"/v1/exports/findings?start={start}&end={end}", headers=leader_headers)
        self.assertEqual(denied_export.status_code, 403)
        connect.assert_not_called()

    def test_cookie_upload_requires_exact_origin_and_session_csrf(self):
        self.client.cookies.set("session", self.token())
        csrf_response = self.client.get("/v1/csrf")
        self.assertEqual(csrf_response.status_code, 200)
        self.assertEqual(csrf_response.headers["cache-control"], "no-store")
        token = csrf_response.json()["csrf_token"]
        upload = {"files": {"audio": ("call.wav", b"synthetic", "audio/wav")}, "data": {"external_ref": "cookie-call"}}
        with patch("app.api.accept_recording") as accept:
            missing = self.client.post("/v1/calls", headers={"Origin": "https://audit.example.test", "Idempotency-Key": "cookie-missing"}, **upload)
            cross = self.client.post("/v1/calls", headers={"Origin": "https://evil.example.test", "X-CSRF-Token": token, "Idempotency-Key": "cookie-cross"}, **upload)
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(cross.status_code, 403)
        accept.assert_not_called()
        with patch("app.api.accept_recording", return_value={"id": "call-a", "processing_state": "QUEUED"}) as accept:
            success = self.client.post(
                "/v1/calls",
                headers={"Origin": "https://audit.example.test", "X-CSRF-Token": token, "Idempotency-Key": "cookie-ok"},
                **upload,
            )
        self.assertEqual(success.status_code, 202)
        accept.assert_called_once()

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

    def test_oversized_content_length_is_rejected_before_intake(self):
        with patch("app.api.accept_recording") as accept:
            response = self.client.post(
                "/v1/calls",
                headers={
                    "Authorization": f"Bearer {self.token()}",
                    "Idempotency-Key": "request-too-large",
                    "Content-Length": str(MAX_AUDIO_BYTES + MULTIPART_OVERHEAD_BYTES + 1),
                },
                content=b"not read",
            )
        self.assertEqual(response.status_code, 413)
        accept.assert_not_called()

    def test_chunked_or_lying_content_length_is_counted_before_parser(self):
        async def exercise():
            delivered = 0
            sent = []

            async def app(_scope, receive, _send):
                nonlocal delivered
                while True:
                    message = await receive()
                    delivered += len(message.get("body", b""))
                    if not message.get("more_body", False):
                        return

            messages = iter((
                {"type": "http.request", "body": b"123", "more_body": True},
                {"type": "http.request", "body": b"456", "more_body": False},
            ))

            async def receive():
                return next(messages)

            async def send(message):
                sent.append(message)

            async def authenticate(_request):
                return Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"}))

            middleware = UploadBodyLimitMiddleware(app, max_bytes=5, get_scope=authenticate, concurrent_requests=1)
            await middleware({"type": "http", "method": "POST", "path": "/v1/calls", "headers": [(b"content-length", b"1")]}, receive, send)
            return delivered, sent

        delivered, sent = asyncio.run(exercise())
        self.assertEqual(delivered, 3)
        self.assertEqual(sent[0]["status"], 413)

    def test_full_upload_slots_reject_without_queueing_or_auth_work(self):
        async def exercise():
            auth_called = False
            sent = []

            async def app(_scope, _receive, _send):
                self.fail("saturated upload reached multipart parser")

            async def authenticate(_request):
                nonlocal auth_called
                auth_called = True
                return Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"}))

            async def receive():
                self.fail("saturated upload read body")

            async def send(message):
                sent.append(message)

            middleware = UploadBodyLimitMiddleware(app, max_bytes=5, get_scope=authenticate, concurrent_requests=1)
            self.assertTrue(middleware.slots.acquire(blocking=False))
            try:
                await middleware({"type": "http", "method": "POST", "path": "/v1/calls", "headers": []}, receive, send)
            finally:
                middleware.slots.release()
            return auth_called, sent

        auth_called, sent = asyncio.run(exercise())
        self.assertFalse(auth_called)
        self.assertEqual(sent[0]["status"], 503)


class IdentityLookupUnitTests(unittest.TestCase):
    def test_identity_database_failure_raises_closed_error(self):
        with patch("app.auth.connect", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(IdentityStoreError):
                resolve_identity_from_db("verified-subject")


@unittest.skipUnless(os.environ.get("DATABASE_URL") or os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class IdentityLookupIntegrationTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("DATABASE_URL"):
            self.fail("RUN_POSTGRES_INTEGRATION=1 requires DATABASE_URL for an isolated PostgreSQL database ending in _test")
        if not (urlparse(os.environ["DATABASE_URL"]).path or "").lstrip("/").endswith("_test"):
            self.fail("Refusing integration test unless DATABASE_URL database name ends in _test")
        migrate()
        self.subject = f"oidc-{uuid4()}"
        self.org_a, self.org_b = uuid4(), uuid4()
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s),(%s)", (self.org_a, self.org_b))
            connection.execute("INSERT INTO identity_memberships(subject,organisation_id,user_id,role,team_id) VALUES (%s,%s,'agent-a','AGENT','team-a'),(%s,%s,'agent-b','AGENT','team-b')", (self.subject, self.org_a, self.subject, self.org_b))

    def tearDown(self):
        with connect() as connection:
            connection.execute("DELETE FROM identity_memberships WHERE subject=%s", (self.subject,))
            connection.execute("DELETE FROM organisations WHERE id=ANY(%s)", ([self.org_a, self.org_b],))

    def test_org_selector_must_match_server_owned_subject_membership(self):
        self.assertIsNone(resolve_identity_from_db(self.subject))
        selected = resolve_identity_from_db(self.subject, str(self.org_a))
        self.assertEqual(selected, Scope(str(self.org_a), "agent-a", "AGENT", frozenset({"team-a"})))
        self.assertIsNone(resolve_identity_from_db(self.subject, str(uuid4())))
        with connect() as connection:
            connection.execute("INSERT INTO identity_memberships(subject,organisation_id,user_id,role,team_id) VALUES (%s,%s,'other-agent','AGENT','team-a')", (self.subject, self.org_a))
        self.assertIsNone(resolve_identity_from_db(self.subject, str(self.org_a)))
