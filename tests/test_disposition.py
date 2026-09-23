import json
import os
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
import unittest
from urllib.parse import urlparse
from uuid import uuid4

from app.disposition import classify_disposition
from app.disposition_config import ConfigError, compile_disposition_config
from app.db import connect
from app.migrate import migrate


def sample_config(**changes):
    raw = {
        "schema_version": "1.0.0",
        "config_id": "retention_v1",
        "version": 1,
        "identity": {"use_case_id": "retention", "display_name": "Synthetic retention disposition"},
        "questions": {
            "committed": {"type": "noul", "instructions": "Was a commitment made?", "criteria": {"true": "Yes", "false": "No"}},
            "next_step": {"type": "choice", "instructions": "Select the next step", "criteria": {"CALLBACK": "Callback", "DECLINED": "Declined"}},
        },
        "taxonomy": {"codes": [
            {"code": "COMMITTED", "name": "Committed", "level": 0, "parent_code": None, "terminal": True, "callback": False, "success": True},
            {"code": "REVIEW", "name": "Review", "level": 0, "parent_code": None, "terminal": True, "callback": False, "success": False},
            {"code": "CALLBACK", "name": "Callback", "level": 0, "parent_code": None, "terminal": True, "callback": True, "success": False},
            {"code": "DECLINED", "name": "Declined", "level": 0, "parent_code": None, "terminal": True, "callback": False, "success": False},
            {"code": "NO_CONTACT", "name": "No contact", "level": 0, "parent_code": None, "terminal": True, "callback": True, "success": False},
        ]},
        "front_gates": [{"id": "NO_CONNECT", "enabled": True, "priority": 1, "source": "telephony_status", "condition": {"operator": "IN", "value": ["NO_ANSWER"]}, "emit": "NO_CONTACT"}],
        "resolver": {"strategy": "FIRST_MATCH_WINS", "default_emit": "REVIEW", "rules": [
            {"id": "COMMIT", "priority": 10, "when": {"all": [{"signal": "committed", "operator": "NOUL_GTE", "value": 0.7}]}, "emit": "COMMITTED"},
            {"id": "CALLBACK", "priority": 20, "when": {"all": [{"signal": "next_step", "operator": "CHOICE_EQ", "value": "CALLBACK"}]}, "emit": "CALLBACK"},
        ]},
        "confidence": {"default_commit_threshold": 0.6, "default_review_threshold": 0.4, "low_confidence_action": "REVIEW"},
        "runtime": {"context_limit_chars": 2000, "window_chars": 1000, "overlap_turns": 0, "max_chunks": 4},
    }
    for key, value in changes.items():
        raw[key] = value
    return raw


class FakeAdapter:
    artifact_version = "sha256:" + "a" * 64
    adapter_version = "fake-v1"

    def __init__(self, *, noul=0.9, choice="CALLBACK", malformed=False):
        self.noul, self.choice, self.malformed = noul, choice, malformed
        self.seen = []

    def evaluate(self, questions, turns):
        self.seen.append((questions, list(turns)))
        if self.malformed:
            return {"invented": {"type": "noul", "p_yes": 0.9, "confidence": 0.9, "evidence_ids": [turns[0]["id"]]}}
        refs = [turns[0]["id"]]
        return {
            "committed": {"type": "noul", "p_yes": self.noul, "confidence": 0.9, "evidence_ids": refs},
            "next_step": {"type": "choice", "value": self.choice, "probabilities": {"CALLBACK": 0.9 if self.choice == "CALLBACK" else 0.1, "DECLINED": 0.1 if self.choice == "CALLBACK" else 0.9}, "confidence": 0.9, "evidence_ids": refs},
        }


class DispositionUnitTests(unittest.TestCase):
    def setUp(self):
        self.config = compile_disposition_config(sample_config())
        self.turns = [
            {"id": "u1", "role": "CUSTOMER", "start_ms": 0, "end_ms": 100, "text_redacted": "I can complete that today.", "is_final": True},
            {"id": "u2", "role": "AGENT", "start_ms": 101, "end_ms": 200, "text_redacted": "Thank you.", "is_final": True},
        ]

    def test_compiler_rejects_identity_or_model_implementation_fields(self):
        for key, value in (("tenant_id", "org-a"), ("provider", {"model": "x"})):
            config = sample_config()
            config[key] = value
            with self.subTest(key=key), self.assertRaises(ConfigError):
                compile_disposition_config(config)
        config = sample_config()
        config["identity"]["tenant_id"] = "org-a"
        with self.assertRaises(ConfigError):
            compile_disposition_config(config)

    def test_checked_in_provider_neutral_example_compiles(self):
        example = json.loads(Path(__file__).resolve().parent.parent.joinpath("config", "disposition.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(compile_disposition_config(example).config_id, "sample_outcome_v1")

    def test_compiler_checks_operator_values_before_runtime(self):
        config = sample_config()
        config["resolver"]["rules"][0]["when"]["all"][0]["value"] = "not-a-number"
        with self.assertRaises(ConfigError):
            compile_disposition_config(config)

    def test_compiler_bounds_config_size_depth_types_and_persisted_integer_ranges(self):
        config = sample_config(); config["questions"]["committed"]["criteria"] = ["wrong-container"]
        with self.assertRaises(ConfigError): compile_disposition_config(config)
        config = sample_config(); config["version"] = 2**31
        with self.assertRaises(ConfigError): compile_disposition_config(config)
        config = sample_config(); config["version"] = 10**1000
        with self.assertRaises(ConfigError): compile_disposition_config(config)
        config = sample_config(); config["resolver"]["rules"][0]["when"]["all"][0]["value"] = 10**1000
        with self.assertRaises(ConfigError): compile_disposition_config(config)
        config = sample_config(); config["resolver"]["rules"][0]["when"]["all"][0]["value"] = 1e308
        with self.assertRaises(ConfigError): compile_disposition_config(config)
        config = sample_config(); config["front_gates"][0]["source"] = ["telephony_status"]
        with self.assertRaises(ConfigError): compile_disposition_config(config)
        config = sample_config(); config["confidence"]["per_code"] = []
        with self.assertRaises(ConfigError): compile_disposition_config(config)
        config = sample_config(); config["identity"]["unexpected"] = {"nested": []}
        cursor = config["identity"]["unexpected"]
        for _ in range(20): cursor["nested"] = {"nested": []}; cursor = cursor["nested"]
        with self.assertRaises(ConfigError): compile_disposition_config(config)
        config = sample_config(); config["identity"]["display_name"] = "x" * (256 * 1024)
        with self.assertRaises(ConfigError): compile_disposition_config(config)
        config = sample_config()
        config["resolver"]["rules"][1]["when"]["all"][0]["value"] = "NOT_A_CHOICE"
        with self.assertRaises(ConfigError):
            compile_disposition_config(config)

    def test_front_gate_precedes_model_and_absent_never_matches_negative(self):
        no_contact = sample_config()
        no_contact["front_gates"] = [{"id": "NOT_CONNECTED", "enabled": True, "priority": 1, "source": "telephony_status", "condition": {"operator": "NE", "value": "CONNECTED"}, "emit": "NO_CONTACT"}]
        config = compile_disposition_config(no_contact)
        adapter = FakeAdapter()
        absent = classify_disposition(self.turns, {}, adapter, config)
        self.assertNotEqual(absent.processing_path, "AUTHORITATIVE_FRONT_GATE")
        self.assertEqual(len(adapter.seen), 1)
        adapter.seen.clear()
        matched = classify_disposition(self.turns, {"telephony_status": "NO_ANSWER"}, adapter, self.config)
        self.assertEqual((matched.code, matched.processing_path), ("NO_CONTACT", "AUTHORITATIVE_FRONT_GATE"))
        self.assertEqual(adapter.seen, [])

    def test_deterministic_priority_and_typed_local_inference(self):
        adapter = FakeAdapter()
        first = classify_disposition(self.turns, {}, adapter, self.config)
        second = classify_disposition(self.turns, {}, FakeAdapter(), self.config)
        self.assertEqual((first.status, first.code, first.matched_rule_id), ("RESOLVED", "COMMITTED", "COMMIT"))
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(set(adapter.seen[0][0]), {"committed", "next_step"})
        self.assertEqual(adapter.seen[0][1][0]["text_redacted"], "I can complete that today.")
        self.assertNotIn("raw_text", adapter.seen[0][1][0])

    def test_unknown_roles_partial_transcripts_and_invalid_model_output_abstain(self):
        adapter = FakeAdapter()
        partial = [dict(self.turns[0], is_final=False)]
        self.assertEqual(classify_disposition(partial, {}, adapter, self.config).review_reason, "INCOMPLETE_TRANSCRIPT")
        unknown = [dict(self.turns[0], role="UNKNOWN")]
        self.assertEqual(classify_disposition(unknown, {}, adapter, self.config).review_reason, "UNCERTAIN_SPEAKER_ROLE")
        invalid = classify_disposition(self.turns, {}, FakeAdapter(malformed=True), self.config)
        self.assertEqual((invalid.status, invalid.review_reason), ("NEEDS_REVIEW", "INVALID_MODEL_OUTPUT"))
        inconsistent = classify_disposition(self.turns, {}, InconsistentChoiceAdapter(), self.config)
        self.assertEqual((inconsistent.status, inconsistent.review_reason), ("NEEDS_REVIEW", "INVALID_MODEL_OUTPUT"))

    def test_windowing_has_a_hard_chunk_ceiling_and_conflicts_review(self):
        config_raw = sample_config()
        config_raw["runtime"].update({"context_limit_chars": 2000, "window_chars": 900, "overlap_turns": 0, "max_chunks": 2})
        config = compile_disposition_config(config_raw)
        turns = [dict(self.turns[0], id=f"u{i}", role="CUSTOMER" if i % 2 == 0 else "AGENT", speaker_id=f"speaker-{i % 2}", text_redacted="x" * 700, start_ms=i * 800, end_ms=i * 800 + 700) for i in range(3)]
        adapter = FakeAdapter()
        result = classify_disposition(turns, {}, adapter, config)
        self.assertEqual(result.review_reason, "CONTEXT_LIMIT")
        self.assertEqual(adapter.seen, [])
        turns = turns[:2]
        result = classify_disposition(turns, {}, FakeAdapter(noul=0.95), config)
        self.assertEqual(result.processing_path, "TURN_AWARE_MAP_AGGREGATE")
        self.assertEqual(result.status, "RESOLVED")
        result = classify_disposition(turns, {}, AlternatingAdapter(), config)
        self.assertEqual(result.review_reason, "CONFLICTING_CHUNK_SIGNALS")

    def test_window_limit_total_context_limit_and_max_chunks_are_distinct_bounds(self):
        raw = sample_config(); raw["runtime"].update({"context_limit_chars": 2000, "window_chars": 500, "overlap_turns": 0, "max_chunks": 2})
        config = compile_disposition_config(raw)
        turns = [dict(self.turns[0], id=f"w{i}", role="CUSTOMER" if i % 2 == 0 else "AGENT", speaker_id=f"speaker-{i % 2}", text_redacted="x" * 400, start_ms=i * 500, end_ms=i * 500 + 400) for i in range(3)]
        adapter = FakeAdapter()
        result = classify_disposition(turns, {}, adapter, config)
        self.assertEqual(result.review_reason, "CONTEXT_LIMIT")
        self.assertEqual(adapter.seen, [])
        raw["runtime"].update({"context_limit_chars": 1000, "window_chars": 900, "max_chunks": 4})
        config = compile_disposition_config(raw)
        two_turns = [dict(self.turns[0], id=f"large{i}", role="CUSTOMER" if i % 2 == 0 else "AGENT", speaker_id=f"speaker-{i % 2}", text_redacted="x" * 700, start_ms=i * 800, end_ms=i * 800 + 700) for i in range(2)]
        self.assertEqual(classify_disposition(two_turns, {}, FakeAdapter(), config).review_reason, "CONTEXT_LIMIT")

    def test_overlap_is_counted_inside_each_window_character_ceiling(self):
        raw = sample_config(); raw["runtime"].update({"context_limit_chars": 2000, "window_chars": 800, "overlap_turns": 1, "max_chunks": 4})
        config = compile_disposition_config(raw)
        turns = [dict(self.turns[0], id=f"o{i}", role="CUSTOMER" if i % 2 == 0 else "AGENT", speaker_id=f"speaker-{i}", text_redacted="x" * size, start_ms=i * 1000, end_ms=i * 1000 + size) for i, size in enumerate((300, 500, 500))]
        adapter = FakeAdapter()
        result = classify_disposition(turns, {}, adapter, config)
        self.assertEqual(result.status, "RESOLVED")
        for _, window in adapter.seen:
            self.assertLessEqual(sum(len(turn["text_redacted"]) for turn in window), config.window_chars)

    def test_adjacent_utterances_from_one_speaker_are_never_split(self):
        raw = sample_config(); raw["runtime"].update({"context_limit_chars": 1000, "window_chars": 800, "overlap_turns": 0, "max_chunks": 4})
        config = compile_disposition_config(raw)
        turns = [
            {"id": "agent-a", "role": "AGENT", "speaker_id": "channel-agent", "start_ms": 0, "end_ms": 10, "text_redacted": "A" * 260, "is_final": True},
            {"id": "agent-b", "role": "AGENT", "speaker_id": "channel-agent", "start_ms": 11, "end_ms": 20, "text_redacted": "B" * 260, "is_final": True},
            {"id": "agent-c", "role": "AGENT", "speaker_id": "channel-agent-2", "start_ms": 21, "end_ms": 30, "text_redacted": "Different speaker.", "is_final": True},
            {"id": "customer-a", "role": "CUSTOMER", "speaker_id": "channel-customer", "start_ms": 31, "end_ms": 40, "text_redacted": "A response.", "is_final": True},
        ]
        adapter = FakeAdapter()
        result = classify_disposition(turns, {}, adapter, config)
        self.assertEqual(result.status, "RESOLVED")
        self.assertEqual(len(adapter.seen[0][1]), 3)
        self.assertEqual(adapter.seen[0][1][0]["utterance_ids"], ["agent-a", "agent-b"])
        self.assertEqual(adapter.seen[0][1][1]["speaker_id"], "channel-agent-2")
        missing_ids = [dict(turn, speaker_id=None) for turn in turns[:2]]
        self.assertEqual(len(classify_disposition(missing_ids, {}, adapter := FakeAdapter(), config).signals), 2)
        self.assertEqual([turn["utterance_ids"] for turn in adapter.seen[0][1]], [["agent-a"], ["agent-b"]])
        raw["runtime"]["window_chars"] = 500
        result = classify_disposition(turns, {}, FakeAdapter(), compile_disposition_config(raw))
        self.assertEqual(result.review_reason, "CONTEXT_LIMIT")

    def test_pinned_local_adapter_rejects_remote_endpoint_and_calls_loopback_only(self):
        from app.local_disposition_adapter import LocalVllmDispositionAdapter
        from app.artifacts import directory_sha256
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "model.bin").write_bytes(b"synthetic pinned artifact")
            digest = directory_sha256(Path(directory))
            with self.assertRaises(ValueError):
                LocalVllmDispositionAdapter(artifact_version=f"sha256:{digest}", artifact_path=directory, base_url="https://models.example/v1")
            response_body = {"choices": [{"message": {"content": json.dumps({"signals": "local"})}}]}
            server_root = {"value": directory}

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    payload = json.dumps({"data": [{"id": "disposition-local", "root": server_root["value"]}]}).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
                def do_POST(self):
                    self.rfile.read(int(self.headers["Content-Length"]))
                    payload = json.dumps(response_body).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
                def log_message(self, *_args): pass

            server = HTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                adapter = LocalVllmDispositionAdapter(artifact_version=f"sha256:{digest}", artifact_path=directory, base_url=f"http://127.0.0.1:{server.server_port}/v1")
                self.assertEqual(adapter.evaluate({}, []), {"signals": "local"})
                from app.local_disposition_adapter import LocalModelUnavailable
                server_root["value"] = str(Path(directory).parent)
                with self.assertRaises(LocalModelUnavailable):
                    adapter.evaluate({}, [])
            finally:
                server.shutdown(); thread.join(); server.server_close()


class DispositionRequestGuardTests(unittest.TestCase):
    def test_admin_auth_precedes_body_and_chunked_body_is_bounded(self):
        from fastapi import HTTPException
        from app.api import DispositionBodyLimitMiddleware

        called = {"app": False, "receive": False}
        async def unauthorized(_request):
            raise HTTPException(status_code=401)
        async def downstream(scope, receive, send):
            called["app"] = True
            await receive()
        async def receive():
            called["receive"] = True
            return {"type": "http.request", "body": b"x", "more_body": False}
        async def send(_message): pass
        middleware = DispositionBodyLimitMiddleware(downstream, get_scope=unauthorized, max_bytes=4)
        scope = {"type": "http", "method": "POST", "path": "/v1/disposition-configs", "headers": [], "state": {}, "asgi": {"version": "3.0"}}
        asyncio.run(middleware(scope, receive, send))
        self.assertFalse(called["app"])
        self.assertFalse(called["receive"])

        class AdminScope:
            role = "ADMIN"
        async def authorized(_request): return AdminScope()
        async def oversized_app(scope, receive, send):
            await receive()
        messages = []
        async def capture(message): messages.append(message)
        async def oversized_receive(): return {"type": "http.request", "body": b"12345", "more_body": False}
        middleware = DispositionBodyLimitMiddleware(oversized_app, get_scope=authorized, max_bytes=4)
        asyncio.run(middleware(scope, oversized_receive, capture))
        self.assertEqual(messages[0]["status"], 413)


class AlternatingAdapter(FakeAdapter):
    def evaluate(self, questions, turns):
        self.seen.append((questions, list(turns)))
        value = 0.95 if turns[0]["id"] == "u0" else 0.05
        refs = [turns[0]["id"]]
        return {"committed": {"type": "noul", "p_yes": value, "confidence": 0.9, "evidence_ids": refs}, "next_step": {"type": "choice", "value": "CALLBACK", "probabilities": {"CALLBACK": 0.9, "DECLINED": 0.1}, "confidence": 0.9, "evidence_ids": refs}}


class InconsistentChoiceAdapter(FakeAdapter):
    def evaluate(self, questions, turns):
        output = super().evaluate(questions, turns)
        output["next_step"]["probabilities"] = {"CALLBACK": 0.1, "DECLINED": 0.9}
        return output


@unittest.skipUnless(os.environ.get("DATABASE_URL") or os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class DispositionPostgresTests(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("DATABASE_URL"):
            self.fail("RUN_POSTGRES_INTEGRATION=1 requires isolated DATABASE_URL ending in _test")
        parsed = urlparse(os.environ["DATABASE_URL"])
        if not (parsed.path or "").lstrip("/").endswith("_test"):
            self.fail("Refusing integration test unless database name ends in _test")
        migrate()
        self.org, self.call = uuid4(), uuid4()
        self.config = sample_config()
        self.config["config_id"] = f"retention_{self.org.hex[:8]}"
        compiled = compile_disposition_config(self.config)
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s)", (self.org,))
            connection.execute("INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state,transcript_revision) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'ANALYSING',1)", (self.org, self.call, "ext", "idem", "a" * 64, "agent", "team", "en"))
            connection.execute("INSERT INTO disposition_config_versions(organisation_id,config_id,use_case_id,version,content_hash,schema_version,raw_json,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)", (self.org, compiled.config_id, compiled.use_case_id, compiled.version, compiled.content_hash, compiled.schema_version, json.dumps(compiled.normalized), "admin"))
            connection.execute("INSERT INTO active_disposition_configs(organisation_id,config_id,version,generation,changed_by) VALUES (%s,%s,%s,1,%s)", (self.org, compiled.config_id, compiled.version, "admin"))
            connection.execute("INSERT INTO disposition_config_events(organisation_id,id,actor_id,action,config_id,version) VALUES (%s,%s,%s,'ACTIVATED',%s,%s)", (self.org, uuid4(), "admin", compiled.config_id, compiled.version))
            connection.execute("INSERT INTO transcript_utterances(organisation_id,call_id,revision,id,segment_id,speaker_id,role,start_ms,end_ms,text_redacted,confidence,model_version,is_final) VALUES (%s,%s,1,'utt1','seg1','channel-0','CUSTOMER',0,100,'I agree to proceed.',0.9,'fake-asr',true)", (self.org, self.call))

    def test_worker_persists_scoped_immutable_revision_and_never_marks_ready(self):
        from app.worker import make_disposition_processor
        from app.ingest import finish_job
        with connect() as connection:
            connection.execute("INSERT INTO jobs(organisation_id,id,call_id,stage,input_revision,state) VALUES (%s,%s,%s,'ANALYSE',1,'QUEUED')", (self.org, uuid4(), self.call))
            job = connection.execute("SELECT * FROM jobs WHERE organisation_id=%s AND call_id=%s", (self.org, self.call)).fetchone()
            columns = [item.name for item in connection.execute("SELECT * FROM jobs LIMIT 0").description]
            job = dict(zip(columns, job, strict=True))
            self.assertEqual((job["input_revision"], str(job["call_id"])), (1, str(self.call)))
            lease_token = uuid4()
            connection.execute("UPDATE jobs SET state='RUNNING',attempts=1,lease_token=%s,lease_until=now()+interval '5 minutes' WHERE organisation_id=%s AND id=%s", (lease_token, self.org, job["id"]))
            job["lease_token"] = lease_token
            connection.execute("INSERT INTO transcript_utterances(organisation_id,call_id,revision,id,segment_id,speaker_id,role,start_ms,end_ms,text_redacted,confidence,model_version,is_final) VALUES (%s,%s,1,'utt2','seg2','channel-1','CUSTOMER',101,200,'I also agree.',0.9,'fake-asr',true)", (self.org, self.call))
            connection.commit()
            adapter = FakeAdapter()
            output = make_disposition_processor(adapter)(job)
            self.assertEqual([turn["speaker_id"] for turn in adapter.seen[0][1]], ["channel-0", "channel-1"])
            self.assertEqual([turn["utterance_ids"] for turn in adapter.seen[0][1]], [["utt1"], ["utt2"]])
            self.assertTrue(finish_job(connection, str(job["id"]), str(job["lease_token"]), output))
            row = connection.execute("SELECT organisation_id,revision,transcript_revision,code,status,model_artifact,signals_json FROM dispositions WHERE organisation_id=%s AND call_id=%s", (self.org, self.call)).fetchone()
            self.assertEqual((str(row[0]), row[1], row[2], row[3], row[4], row[5]), (str(self.org), 1, 1, "COMMITTED", "RESOLVED", "sha256:" + "a" * 64))
            self.assertIn("committed", row[6])
            self.assertEqual(connection.execute("SELECT processing_state FROM calls WHERE organisation_id=%s AND id=%s", (self.org, self.call)).fetchone()[0], "NEEDS_REVIEW")
        from psycopg.errors import RaiseException
        with connect() as connection:
            with self.assertRaises(RaiseException):
                connection.execute("UPDATE dispositions SET code='DECLINED' WHERE organisation_id=%s AND call_id=%s", (self.org, self.call))
        with connect() as connection:
            connection.execute("UPDATE calls SET transcript_revision=2,processing_state='ANALYSING' WHERE organisation_id=%s AND id=%s", (self.org, self.call))
            connection.execute("INSERT INTO transcript_utterances(organisation_id,call_id,revision,id,segment_id,speaker_id,role,start_ms,end_ms,text_redacted,confidence,model_version,is_final) VALUES (%s,%s,2,'utt2','seg2','channel-0','CUSTOMER',0,120,'I agree to proceed tomorrow.',0.9,'fake-asr-v1',true)", (self.org, self.call))
            connection.execute("INSERT INTO jobs(organisation_id,id,call_id,stage,input_revision,state) VALUES (%s,%s,%s,'ANALYSE',2,'RUNNING')", (self.org, uuid4(), self.call))
            job = connection.execute("SELECT * FROM jobs WHERE organisation_id=%s AND call_id=%s AND input_revision=2", (self.org, self.call)).fetchone()
            columns = [item.name for item in connection.execute("SELECT * FROM jobs LIMIT 0").description]
            job = dict(zip(columns, job, strict=True))
            self.assertEqual((job["input_revision"], str(job["call_id"])), (2, str(self.call)))
            token = uuid4()
            connection.execute("UPDATE jobs SET lease_token=%s,lease_until=now()+interval '5 minutes' WHERE organisation_id=%s AND id=%s", (token, self.org, job["id"]))
            snapshot = connection.execute("SELECT c.transcript_revision,a.config_id,a.version FROM calls c LEFT JOIN active_disposition_configs a ON a.organisation_id=c.organisation_id WHERE c.organisation_id=%s AND c.id=%s", (self.org, self.call)).fetchone()
            self.assertEqual(snapshot, (2, self.config["config_id"], 1))
            query_snapshot = connection.execute("SELECT v.config_id,v.version,v.raw_json,c.transcript_revision FROM active_disposition_configs a JOIN disposition_config_versions v ON v.organisation_id=a.organisation_id AND v.config_id=a.config_id AND v.version=a.version JOIN calls c ON c.organisation_id=a.organisation_id WHERE a.organisation_id=%s AND c.id=%s AND c.tombstoned_at IS NULL", (job["organisation_id"], job["call_id"])).fetchone()
            self.assertEqual(query_snapshot[3], job["input_revision"])
            connection.commit()
            output = make_disposition_processor(FakeAdapter())(job)
            self.assertTrue(finish_job(connection, str(job["id"]), str(token), output))
            revisions = connection.execute("SELECT revision,transcript_revision,code FROM dispositions WHERE organisation_id=%s AND call_id=%s ORDER BY revision", (self.org, self.call)).fetchall()
            self.assertEqual(revisions, [(1, 1, "COMMITTED"), (2, 2, "COMMITTED")])

    def test_config_events_and_records_are_tenant_scoped(self):
        other_org = uuid4()
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s)", (other_org,))
            self.assertIsNone(connection.execute("SELECT 1 FROM disposition_config_versions WHERE organisation_id=%s AND config_id=%s", (other_org, "retention_v1")).fetchone())
            self.assertEqual(connection.execute("SELECT count(*) FROM disposition_config_events WHERE organisation_id=%s", (other_org,)).fetchone()[0], 0)

    def test_admin_config_lifecycle_is_scoped_and_uses_generation_cas(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        from fastapi.testclient import TestClient
        from app.api import create_app
        from app.auth import Scope
        from app.config import Settings

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        api_org, other_org = uuid4(), uuid4()
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s)", (api_org,))
            connection.execute("INSERT INTO organisations(id) VALUES (%s)", (other_org,))
        scopes = {"admin": Scope(str(api_org), "admin-user", "ADMIN", frozenset()), "approver": Scope(str(api_org), "approver-user", "ADMIN", frozenset()), "other": Scope(str(other_org), "other-admin", "ADMIN", frozenset()), "qa": Scope(str(api_org), "qa-user", "QA_ANALYST", frozenset())}
        settings = Settings(oidc_issuer="https://identity.test", oidc_audience="audit", oidc_public_key=public_key, csrf_secret="test-only-csrf-secret-at-least-32-bytes-long", allowed_origins=("https://audit.test",))
        client = TestClient(create_app(settings, identity_lookup=scopes.get, disposition_adapter=FakeAdapter()))
        def headers(subject="admin"):
            token = jwt.encode({"sub": subject, "iss": settings.oidc_issuer, "aud": settings.oidc_audience, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, private_key, algorithm="RS256")
            return {"Authorization": f"Bearer {token}"}
        try:
            candidate = sample_config()
            config_id = candidate["config_id"]
            self.assertEqual(client.post("/v1/disposition-configs/validate", json=candidate, headers=headers()).status_code, 200)
            self.assertEqual(client.post("/v1/disposition-configs", json=candidate, headers=headers()).status_code, 201)
            compiled = compile_disposition_config(candidate)
            call_id = uuid4()
            with connect() as connection:
                connection.execute("INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state,transcript_revision) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'NEEDS_REVIEW',1)", (api_org, call_id, f"disp-{call_id}", f"idem-{call_id}", "b" * 64, "agent", "team", "en"))
                connection.execute("INSERT INTO dispositions(organisation_id,id,call_id,revision,transcript_revision,config_id,config_version,config_hash,schema_version,model_artifact,adapter_version,processing_path,status,code,parent_code,confidence,requires_review,matched_rule_id,signals_json,usage_json) VALUES (%s,%s,%s,1,1,%s,1,%s,%s,'fake-artifact','fake-v1','SINGLE_PASS','RESOLVED','COMMITTED',NULL,0.9,false,'COMMIT','{}'::jsonb,'{}'::jsonb)", (api_org, uuid4(), call_id, config_id, compiled.content_hash, compiled.schema_version))
            self.assertEqual(client.get(f"/v1/calls/{call_id}/disposition", headers=headers()).json()["code"], "COMMITTED")
            self.assertEqual(client.get(f"/v1/calls/{call_id}/disposition", headers=headers("other")).status_code, 404)
            self.assertEqual(client.post("/v1/disposition-configs", json=candidate, headers=headers()).status_code, 409)
            self.assertEqual(client.post(f"/v1/disposition-configs/{config_id}/versions/1/approve", json={"reason": "self approval"}, headers=headers()).status_code, 403)
            self.assertEqual(client.post(f"/v1/disposition-configs/{config_id}/versions/1/activate", json={"expected_generation": 0}, headers=headers()).status_code, 409)
            approved = client.post(f"/v1/disposition-configs/{config_id}/versions/1/approve", json={"reason": "independent policy review"}, headers=headers("approver"))
            self.assertEqual((approved.status_code, approved.json().get("approved_by")), (200, "approver-user"), approved.text)
            listed = client.get("/v1/disposition-configs", headers=headers()).json()["versions"]
            self.assertEqual(next(item for item in listed if item["config_id"] == config_id and item["version"] == 1)["approved_by"], "approver-user")
            activated = client.post(f"/v1/disposition-configs/{config_id}/versions/1/activate", json={"expected_generation": 0}, headers=headers())
            self.assertEqual(activated.status_code, 200, activated.text)
            self.assertEqual(activated.json()["generation"], 1)
            self.assertEqual(client.post(f"/v1/disposition-configs/{config_id}/versions/1/activate", json={"expected_generation": 0}, headers=headers()).status_code, 409)
            second = sample_config(); second["version"] = 2
            self.assertEqual(client.post("/v1/disposition-configs", json=second, headers=headers()).status_code, 201)
            self.assertEqual(client.post(f"/v1/disposition-configs/{config_id}/versions/2/approve", json={"reason": "reviewed v2"}, headers=headers("approver")).status_code, 200)
            self.assertEqual(client.post(f"/v1/disposition-configs/{config_id}/versions/2/activate", json={"expected_generation": 1}, headers=headers()).json()["generation"], 2)
            replay = client.post(f"/v1/disposition-configs/{config_id}/versions/1/replay", json={"fixture_set_id": "synthetic-smoke-v1"}, headers=headers())
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["sample_count"], 1)
            rollback = client.post(f"/v1/disposition-configs/{config_id}/rollback", json={"version": 1, "expected_generation": 2, "reason": "synthetic rollback test"}, headers=headers())
            self.assertEqual((rollback.status_code, rollback.json().get("generation")), (200, 3))
            self.assertEqual(client.get("/v1/disposition-configs", headers=headers("other")).json()["versions"], [])
            self.assertEqual(client.post("/v1/disposition-configs/validate", json=candidate, headers=headers("qa")).status_code, 403)
            with connect() as connection:
                applied = connection.execute("SELECT 1 FROM schema_migrations WHERE version=6").fetchone()
                check = connection.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='disposition_config_events'::regclass AND conname='disposition_config_events_action_check'").fetchone()
                self.assertIsNotNone(applied)
                self.assertIn("APPROVED", check[0])
                active = connection.execute("SELECT config_id,version,generation FROM active_disposition_configs WHERE organisation_id=%s", (api_org,)).fetchone()
                self.assertEqual(tuple(active), (config_id, 1, 3))
                events = connection.execute("SELECT action,reason FROM disposition_config_events WHERE organisation_id=%s AND config_id=%s ORDER BY created_at,id", (api_org, config_id)).fetchall()
                self.assertIn(("ROLLBACK", "synthetic rollback test"), events)
                self.assertIn(("APPROVED", "independent policy review"), events)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
