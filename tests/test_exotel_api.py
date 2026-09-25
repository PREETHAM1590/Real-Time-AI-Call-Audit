import base64
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.websockets import WebSocketDisconnect

from app.api import create_app
from app.auth import Scope
from app.config import Settings
from app.exotel_adapter import integration_credentials, make_audio_references
from app.transcription import LiveWindowTranscriber


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
        self.session_incomplete = threading.Event()

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
            if params[0] == "INCOMPLETE":
                self.session_incomplete.set()
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

    def test_sub_millisecond_frames_do_not_accumulate_timestamp_drift(self):
        # 12 samples = 1.5 ms per frame; provider offsets follow true elapsed time (0, 1.5, 3.0 ms, floored).
        with patch("app.api.connect", return_value=self.connection), patch("app.api.accept_recording") as intake:
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                self._send_start(ws)
                for index, offset in enumerate((0, 1, 3)):
                    ws.send_text(json.dumps({"event": "media", "sequence_number": index + 2, "stream_sid": "stream-a",
                                             "media": {"chunk": index + 1, "timestamp": str(offset),
                                                       "payload": base64.b64encode(b"\x01\x00" * 12).decode()}}))
                ws.send_text(json.dumps({"event": "stop", "sequence_number": 5, "stream_sid": "stream-a",
                                         "stop": {"call_sid": "call-a", "account_sid": "acct-a", "reason": "callended"}}))
                result = ws.receive()
        self.assertEqual(result["code"], 1000)
        self.assertEqual(intake.call_count, 1)

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
        self.assertEqual(self.connection.session_states, ["DISABLED", "DRAINING"])

    def test_empty_clean_stop_is_rejected_without_intake(self):
        with patch("app.api.connect", return_value=self.connection), patch("app.api.accept_recording") as intake:
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                self._send_start(ws)
                ws.send_text(json.dumps({"event": "stop", "sequence_number": 2, "stream_sid": "stream-a",
                                        "stop": {"call_sid": "call-a", "account_sid": "acct-a", "reason": "callended"}}))
                closed = ws.receive()
            self.assertEqual((closed["type"], closed["code"]), ("websocket.close", 4400))
            intake.assert_not_called()
        self.assertEqual(self.connection.session_states, ["DISABLED", "DRAINING", "INCOMPLETE"])

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
            self.assertEqual(self.connection.session_states, ["DISABLED", "INCOMPLETE"])

        self.connection.session_activated.clear()
        with patch("app.api.connect", return_value=self.connection), patch("app.api.EXOTEL_MEDIA_IDLE_TIMEOUT_SECONDS", 2), patch("app.api.EXOTEL_SESSION_TIMEOUT_SECONDS", 1), patch("app.api.accept_recording") as intake:
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                self._send_start(ws)
                self.assertTrue(self.connection.session_activated.wait(1))
                self.assertEqual(ws.receive()["code"], 4408)
            self.assertEqual(intake.call_count, 0)
            self.assertEqual(self.connection.session_states, ["DISABLED", "INCOMPLETE", "DISABLED", "INCOMPLETE"])

    def test_clean_stop_near_session_deadline_reserves_time_for_recording_intake(self):
        class SlowPreview:
            def push(self, _pcm, _start_ms):
                return []

            def finish(self):
                time.sleep(0.8)
                return []

            def discard(self):
                pass

        with patch.dict("os.environ", {"EXOTEL_LIVE_TRANSCRIPTION": "1"}), \
                patch("app.api.connect", return_value=self.connection), \
                patch("app.api.EXOTEL_SESSION_TIMEOUT_SECONDS", 2), \
                patch("app.api.EXOTEL_LIVE_TRANSCRIPTION_DRAIN_SECONDS", 1), \
                patch("app.api.EXOTEL_RECORDING_INTAKE_TIMEOUT_SECONDS", 1), \
                patch("app.api.exotel_live_transcriber_from_environment", return_value=SlowPreview()), \
                patch("app.api.update_live_transcription_state", return_value=True), \
                patch("app.api.store_live_utterances"), \
                patch("app.api.accept_recording") as intake:
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                self._send_start(ws)
                ws.send_text(json.dumps({"event": "media", "sequence_number": 2, "stream_sid": "stream-a",
                                         "media": {"chunk": 1, "timestamp": "0", "payload": base64.b64encode(b"\x01\x00" * 1_600).decode()}}))
                time.sleep(1.5)
                ws.send_text(json.dumps({"event": "stop", "sequence_number": 3, "stream_sid": "stream-a",
                                         "stop": {"call_sid": "call-a", "account_sid": "acct-a", "reason": "callended"}}))
                result = ws.receive()
        self.assertEqual(result["code"], 1000)
        self.assertEqual(intake.call_count, 1)
        self.assertTrue(intake.call_args.args[2].startswith(b"RIFF"))
        self.assertGreater(intake.call_args.kwargs["timeout_seconds"], 0)

    def test_intake_failure_after_clean_stop_stays_incomplete(self):
        from app.ingest import IntakeError

        with patch("app.api.connect", return_value=self.connection), patch("app.api.accept_recording", side_effect=IntakeError("synthetic")):
            result = self._stream()
        self.assertEqual(result["code"], 4409)
        self.assertEqual(self.connection.session_states, ["DISABLED", "DRAINING", "INCOMPLETE"])

    def test_disconnect_marks_active_generation_incomplete(self):
        with patch("app.api.connect", return_value=self.connection), patch("app.api.accept_recording") as intake:
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                self._send_start(ws)
                self.assertTrue(self.connection.session_activated.wait(1))
                ws.close()
        # Bounded, not tight: this only waits as long as it takes for the background
        # disconnect-cleanup task to actually run, so a higher ceiling never slows the
        # normal passing case. Raised from 5s after a shared CI runner missed that
        # window once under load (GitHub Actions run 36048260809) while every other
        # assertion in this suite passed; 3 subsequent runs passed within the old bound.
        self.assertTrue(self.connection.session_incomplete.wait(20))
        self.assertEqual(intake.call_count, 0)
        self.assertEqual(self.connection.session_states, ["DISABLED", "INCOMPLETE"])

    def test_live_transcription_publishes_only_redacted_unknown_utterances(self):
        class FakeTranscriber:
            def push(self, pcm, start_ms):
                self.pcm, self.start_ms = pcm, start_ms
                return [SimpleNamespace(id="a" * 32, role="UNKNOWN", start_ms=0, end_ms=100,
                                        text_redacted="number [REDACTED]")]

            def finish(self):
                return []

            def discard(self):
                pass

        stored, statuses = [], []
        with patch.dict("os.environ", {"EXOTEL_LIVE_TRANSCRIPTION": "1"}), \
                patch("app.api.connect", return_value=self.connection), \
                patch("app.api.exotel_live_transcriber_from_environment", return_value=FakeTranscriber()), \
                patch("app.api.store_live_utterances", side_effect=lambda *args, **kwargs: stored.extend(args[5])), \
                patch("app.api.update_live_transcription_state", side_effect=lambda _conn, _org, _integration, _key, _generation, state: statuses.append(state)) as update, \
                patch("app.api.accept_recording") as intake:
            result = self._stream()
        self.assertEqual(result["code"], 1000)
        self.assertEqual(intake.call_count, 1)
        self.assertTrue(stored)
        self.assertEqual(stored[0]["role"], "UNKNOWN")
        self.assertEqual(stored[0]["text"], "number [REDACTED]")
        self.assertNotIn("555", json.dumps(stored))
        self.assertIn("EMPTY", statuses)
        self.assertEqual(update.call_args_list[-1].args[-1], "EMPTY")

    def test_live_inference_failure_degrades_preview_without_losing_recording(self):
        statuses = []
        with patch.dict("os.environ", {"EXOTEL_LIVE_TRANSCRIPTION": "1"}), \
                patch("app.api.connect", return_value=self.connection), \
                patch("app.api.exotel_live_transcriber_from_environment", side_effect=RuntimeError("synthetic raw failure")), \
                patch("app.api.update_live_transcription_state", side_effect=lambda _conn, _org, _integration, _key, _generation, state: statuses.append(state)), \
                patch("app.api.accept_recording") as intake:
            result = self._stream()
        self.assertEqual(result["code"], 1000)
        self.assertEqual(intake.call_count, 1)
        self.assertIn("DEGRADED", statuses)

    def test_live_preview_storage_failure_still_accepts_durable_recording(self):
        class FakeTranscriber:
            def push(self, _pcm, _start_ms):
                return [SimpleNamespace(id="b" * 32, role="UNKNOWN", start_ms=0, end_ms=100,
                                        text_redacted="synthetic redacted")]

            def finish(self):
                return []

            def discard(self):
                pass

        statuses = []
        with patch.dict("os.environ", {"EXOTEL_LIVE_TRANSCRIPTION": "1"}), \
                patch("app.api.connect", return_value=self.connection), \
                patch("app.api.exotel_live_transcriber_from_environment", return_value=FakeTranscriber()), \
                patch("app.api.store_live_utterances", side_effect=RuntimeError("synthetic storage failure")), \
                patch("app.api.update_live_transcription_state", side_effect=lambda _conn, _org, _integration, _key, _generation, state: statuses.append(state)), \
                patch("app.api.accept_recording") as intake:
            result = self._stream()
        self.assertEqual(result["code"], 1000)
        self.assertEqual(intake.call_count, 1)
        self.assertIn("DEGRADED", statuses)

    def test_disconnect_discards_live_transcriber_state_and_keeps_generation_incomplete(self):
        class FakeTranscriber:
            def __init__(self):
                self.pushed = threading.Event()
                self.discarded = threading.Event()

            def push(self, _pcm, _start_ms):
                self.pushed.set()
                return []

            def finish(self):
                return []

            def discard(self):
                self.discarded.set()

        transcriber = FakeTranscriber()
        with patch.dict("os.environ", {"EXOTEL_LIVE_TRANSCRIPTION": "1"}), \
                patch("app.api.connect", return_value=self.connection), \
                patch("app.api.exotel_live_transcriber_from_environment", return_value=transcriber), \
                patch("app.api.update_live_transcription_state", return_value=True), \
                patch("app.api.accept_recording") as intake:
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                self._send_start(ws)
                ws.send_text(json.dumps({"event": "media", "sequence_number": 2, "stream_sid": "stream-a",
                                         "media": {"chunk": 1, "timestamp": "0", "payload": base64.b64encode(b"\x01\x00" * 800).decode()}}))
                self.assertTrue(transcriber.pushed.wait(1))
                ws.close()
        self.assertTrue(transcriber.discarded.wait(1))
        self.assertTrue(self.connection.session_incomplete.wait(1))
        intake.assert_not_called()

    def test_live_queue_overflow_degrades_preview_but_keeps_full_recording_intake(self):
        class FakeTranscriber:
            def push(self, _pcm, _start_ms):
                return []

            def finish(self):
                return []

            def discard(self):
                pass

        def delayed_factory():
            threading.Event().wait(0.2)
            return FakeTranscriber()

        statuses = []
        with patch.dict("os.environ", {"EXOTEL_LIVE_TRANSCRIPTION": "1"}), \
                patch("app.api.connect", return_value=self.connection), \
                patch("app.api.exotel_live_transcriber_from_environment", side_effect=delayed_factory), \
                patch("app.api.update_live_transcription_state", side_effect=lambda _conn, _org, _integration, _key, _generation, state: statuses.append(state)), \
                patch("app.api.accept_recording") as intake:
            with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                self._send_start(ws)
                ws.send_text(json.dumps({"event": "media", "sequence_number": 2, "stream_sid": "stream-a",
                                         "media": {"chunk": 1, "timestamp": "0", "payload": base64.b64encode(b"\x01\x00" * 50_000).decode()}}))
                ws.send_text(json.dumps({"event": "stop", "sequence_number": 3, "stream_sid": "stream-a",
                                         "stop": {"call_sid": "call-a", "account_sid": "acct-a", "reason": "callended"}}))
                result = ws.receive()
        self.assertEqual(result["code"], 1000)
        self.assertEqual(intake.call_count, 1)
        self.assertIn("DEGRADED", statuses)

    def test_hung_live_model_cannot_block_durable_intake_or_websocket_cleanup(self):
        model_started = threading.Event()
        release_model = threading.Event()
        intake_called = threading.Event()

        transcriber = LiveWindowTranscriber(object(), language="en", redact=str)

        def hanging_decode(_pcm, *, final):
            model_started.set()
            release_model.wait()
            return []

        transcriber._decode_window = hanging_decode

        def accept(*_args, **_kwargs):
            intake_called.set()

        try:
            with patch.dict("os.environ", {"EXOTEL_LIVE_TRANSCRIPTION": "1"}), \
                    patch("app.api.connect", return_value=self.connection), \
                    patch("app.api.EXOTEL_LIVE_TRANSCRIPTION_DRAIN_SECONDS", 0.05), \
                    patch("app.api.exotel_live_transcriber_from_environment", return_value=transcriber), \
                    patch("app.api.update_live_transcription_state", return_value=True), \
                    patch("app.api.store_live_utterances"), \
                    patch("app.api.accept_recording", side_effect=accept) as intake:
                with self.client.websocket_connect("/v1/exotel/stream", headers={"Authorization": self._auth_header()}) as ws:
                    self._send_start(ws)
                    ws.send_text(json.dumps({"event": "media", "sequence_number": 2, "stream_sid": "stream-a",
                                             "media": {"chunk": 1, "timestamp": "0", "payload": base64.b64encode(b"\x01\x00" * 32_000).decode()}}))
                    self.assertTrue(model_started.wait(2))
                    ws.send_text(json.dumps({"event": "stop", "sequence_number": 3, "stream_sid": "stream-a",
                                             "stop": {"call_sid": "call-a", "account_sid": "acct-a", "reason": "callended"}}))
                    result = ws.receive()
                    self.assertTrue(intake_called.is_set())
                    self.assertEqual(intake.call_count, 1)
                    self.assertEqual(result["code"], 1000)
                    self.assertFalse(release_model.is_set(), "preview inference must still be blocked when the route closes")
                    self.assertTrue(transcriber._discard_requested.is_set())
                    self.assertEqual(transcriber.buffered_bytes, 0)
                    # Keep the fake genuinely hung until the route has closed, then release it
                    # before Starlette shuts down the portal's default executor.
                    release_model.set()
                    self.assertTrue(transcriber._state_lock.acquire(timeout=1))
                    transcriber._state_lock.release()
        finally:
            release_model.set()

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
