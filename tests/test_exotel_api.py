import base64
import json
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.websockets import WebSocketDisconnect

from app.api import create_app
from app.auth import Scope
from app.config import Settings
from app.exotel_adapter import integration_credentials, make_audio_references


class _Result:
    def __init__(self, one=None, many=None):
        self.one = one
        self.many = many if many is not None else ([] if one is None else [one])

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.session_params = None
        self.session_states = []
        self.session_activated = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def transaction(self):
        return self

    def execute(self, query, params=()):
        if "WHERE username=%s" in query:
            return _Result(self.rows["integration"])
        if "JOIN exotel_agent_mappings" in query or "FROM exotel_agent_mappings" in query:
            return _Result(("agent-a", "team-a"))
        if "FROM identity_memberships" in query:
            return _Result(many=[("team-a",)])
        if "INSERT INTO exotel_sessions" in query:
            self.session_params = params
            self.session_activated.set()
            return _Result((4,))
        if "UPDATE exotel_sessions" in query:
            self.session_states.append(params[0])
            return _Result((params[-1],))
        raise AssertionError("unexpected SQL in synthetic websocket test")


class ExotelWebSocketTests(unittest.TestCase):
    def setUp(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        public_key = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.settings = Settings(oidc_issuer="https://identity.test", oidc_audience="audit", oidc_public_key=public_key, csrf_secret="test-only-csrf-secret-at-least-32-bytes-long", allowed_origins=("https://audit.test",))
        self.client = TestClient(create_app(self.settings, identity_lookup=lambda _: Scope("org-a", "admin-a", "ADMIN", frozenset())))
        self.username, self.password, self.salt, self.verifier = integration_credentials()
        self.rows = {"integration": ("org-a", "integration-a", "acct-a", self.salt, self.verifier)}
        self.connection = _Connection(self.rows)

    def tearDown(self):
        self.client.close()

    def _stream(self, *, secret=None, media_sequence=2):
        password = secret or self.password
        auth = "Basic " + base64.b64encode(f"{self.username}:{password}".encode()).decode()
        try:
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": auth}) as ws:
                ws.send_text(json.dumps({"event": "connected"}))
                ws.send_text(json.dumps({
                    "event": "start", "sequence_number": 1, "stream_sid": "stream-a",
                    "start": {"stream_sid": "stream-a", "call_sid": "call-a", "account_sid": "acct-a",
                              "from": "synthetic-from", "to": "synthetic-to", "custom_parameters": {"agent_ref": "external-agent"},
                              "media_format": {"encoding": "raw", "sample_rate": 8000, "channels": 1, "bit_rate": 16}},
                }))
                ws.send_text(json.dumps({"event": "media", "sequence_number": media_sequence, "stream_sid": "stream-a",
                                         "media": {"chunk": 1, "timestamp": "0", "payload": base64.b64encode(b"\x01\x00" * 800).decode()}}))
                ws.send_text(json.dumps({"event": "stop", "sequence_number": media_sequence + 1, "stream_sid": "stream-a",
                                         "stop": {"call_sid": "call-a", "account_sid": "acct-a", "reason": "callended"}}))
                return ws.receive()
        except WebSocketDisconnect as error:
            return {"type": "websocket.close", "code": error.code}

    def _auth_header(self):
        return "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()

    def _send_start(self, ws):
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(json.dumps({
            "event": "start", "sequence_number": 1, "stream_sid": "stream-a",
            "start": {"stream_sid": "stream-a", "call_sid": "call-a", "account_sid": "acct-a",
                      "custom_parameters": {"agent_ref": "external-agent"},
                      "media_format": {"encoding": "raw", "sample_rate": 8000, "channels": 1, "bit_rate": 16}},
        }))

    def test_clean_synthetic_stream_submits_only_scoped_wav_to_intake(self):
        with patch("app.api.connect", return_value=self.connection), patch("app.api.accept_recording") as intake:
            result = self._stream()
        self.assertEqual(result["type"], "websocket.close")
        self.assertEqual(result["code"], 1000)
        self.assertEqual(intake.call_count, 1)
        scope, external_ref, audio, metadata, idempotency_key = intake.call_args.args
        self.assertEqual(scope, Scope("org-a", "agent-a", "AGENT", frozenset({"team-a"})))
        self.assertEqual((metadata, len(audio) > 44, external_ref == idempotency_key), ({"language": "und"}, True, True))
        call_key = make_audio_references("org-a", "acct-a", "call-a")[0].partition(":")[2]
        self.assertEqual(self.connection.session_params[2], call_key)
        self.assertEqual(intake.call_args.kwargs["generation_fence"], ("integration-a", call_key, 4, "external-agent", "agent-a", "team-a"))
        self.assertEqual(self.connection.session_states, ["DRAINING"])

    def test_gap_and_wrong_password_never_submit_audio(self):
        with patch("app.api.connect", return_value=self.connection), patch("app.api.accept_recording") as intake:
            result = self._stream(media_sequence=3)
            self.assertEqual(result["code"], 4400)
            self.assertEqual(intake.call_count, 0)
            self.assertEqual(self.connection.session_states[-1], "INCOMPLETE")
            result = self._stream(secret="wrong-secret")
            self.assertEqual(result["code"], 4401)
            self.assertEqual(intake.call_count, 0)

    def test_handshake_media_idle_and_total_timeouts_close_sessions(self):
        with patch("app.api.connect", return_value=self.connection), patch("app.api.EXOTEL_HANDSHAKE_TIMEOUT_SECONDS", 0.02):
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                self.assertEqual(ws.receive()["code"], 4408)

        with patch("app.api.connect", return_value=self.connection), patch("app.api.EXOTEL_MEDIA_IDLE_TIMEOUT_SECONDS", 0.02), patch("app.api.EXOTEL_SESSION_TIMEOUT_SECONDS", 1), patch("app.api.accept_recording") as intake:
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                self._send_start(ws)
                self.assertEqual(ws.receive()["code"], 4408)
            self.assertEqual(intake.call_count, 0)
            self.assertEqual(self.connection.session_states, ["INCOMPLETE"])

        self.connection.session_activated.clear()
        with patch("app.api.connect", return_value=self.connection), patch("app.api.EXOTEL_MEDIA_IDLE_TIMEOUT_SECONDS", 2), patch("app.api.EXOTEL_SESSION_TIMEOUT_SECONDS", 1), patch("app.api.accept_recording") as intake:
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                self._send_start(ws)
                self.assertTrue(self.connection.session_activated.wait(1))
                self.assertEqual(ws.receive()["code"], 4408)
            self.assertEqual(intake.call_count, 0)
            self.assertEqual(self.connection.session_states, ["INCOMPLETE", "INCOMPLETE"])

    def test_intake_failure_after_clean_stop_stays_incomplete(self):
        from app.ingest import IntakeError

        with patch("app.api.connect", return_value=self.connection), patch("app.api.accept_recording", side_effect=IntakeError("synthetic")):
            result = self._stream()
        self.assertEqual(result["code"], 4409)
        self.assertEqual(self.connection.session_states, ["DRAINING", "INCOMPLETE"])

    def test_disconnect_marks_active_generation_incomplete(self):
        with patch("app.api.connect", return_value=self.connection), patch("app.api.accept_recording") as intake:
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                self._send_start(ws)
                self.assertTrue(self.connection.session_activated.wait(1))
                ws.close()
        self.assertEqual(intake.call_count, 0)
        self.assertEqual(self.connection.session_states, ["INCOMPLETE"])

    def test_admission_rejects_before_authentication_work_when_full(self):
        with patch("app.api._EXOTEL_CONNECTIONS", threading.Semaphore(0)), patch("app.api.connect") as connect:
            try:
                with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                    result = ws.receive()
            except WebSocketDisconnect as error:
                result = {"type": "websocket.close", "code": error.code}
            self.assertEqual(result["code"], 4429)
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
