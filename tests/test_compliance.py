import json
import hashlib
import copy
import os
from pathlib import Path
import unittest
from urllib.parse import urlparse
from uuid import uuid4

from app.compliance import RulesetError, _opening_deadline, compile_ruleset, disclosure_state, evaluate_rules, scan_sensitive_numbers
from app.contracts import Utterance
from app.db import connect
from app.ingest import finish_job, persist_policy_findings
from app.migrate import migrate


def u(uid, role, start, end, text, *, final=True):
    return Utterance(id=uid, role=role, start_ms=start, end_ms=end, text_redacted=text, is_final=final)


def context(**changes):
    value = {
        "organisation_id": "org-synthetic",
        "call_id": "call-synthetic",
        "transcript_revision": 1,
        "call_type": "synthetic",
        "agent_connected_ms": 0,
        "call_duration_ms": 31_000,
        "holds": [],
        "complete": True,
        "timing_reliable": True,
    }
    value.update(changes)
    return value


class DisclosureStateTests(unittest.TestCase):
    def test_deadline_short_call_and_unreliable_role_states(self):
        self.assertEqual(disclosure_state([], 29_000, ended=False, reliable=True), "PENDING")
        self.assertEqual(disclosure_state([], 30_000, ended=False, reliable=True), "POTENTIAL_VIOLATION")
        self.assertEqual(disclosure_state([], 10_000, ended=True, reliable=True), "UNKNOWN")
        self.assertEqual(disclosure_state([], 30_000, ended=True, reliable=False), "UNKNOWN")
        customer = u("u1", "CUSTOMER", 0, 2_000, "This call may be recorded.")
        self.assertEqual(disclosure_state([customer], 30_000, ended=True, reliable=True), "POTENTIAL_VIOLATION")

    def test_final_agent_phrase_can_span_adjacent_segments(self):
        rows = [
            u("a1", "AGENT", 1_000, 1_500, "This call may be"),
            u("a2", "AGENT", 1_501, 2_000, " recorded for quality purposes."),
        ]
        self.assertEqual(disclosure_state(rows, 5_000, ended=True, reliable=True), "SATISFIED")
        partial = [u("p1", "AGENT", 1_000, 2_000, "This call may be", final=False)]
        self.assertEqual(disclosure_state(partial, 30_000, ended=True, reliable=True), "UNKNOWN")
        interrupted = [
            u("a1", "AGENT", 1_000, 1_500, "This call may be"),
            u("c1", "CUSTOMER", 1_501, 1_700, "Okay."),
            u("a2", "AGENT", 1_701, 2_000, "recorded."),
        ]
        self.assertEqual(disclosure_state(interrupted, 30_000, ended=True, reliable=True), "POTENTIAL_VIOLATION")

    def test_partial_utterance_is_not_durable_disclosure_evidence(self):
        partial = [u("partial", "AGENT", 1_000, 2_000, "This call may be recorded", final=False)]
        self.assertEqual(disclosure_state(partial, 30_000, ended=True, reliable=True), "UNKNOWN")


class PolicyWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ruleset = json.loads(Path(__file__).resolve().parent.parent.joinpath("config", "rules.v1.json").read_text(encoding="utf-8"))

    def test_caller_clips_late_phrase_and_extends_deadline_for_tagged_hold(self):
        rows = [
            u("late", "AGENT", 40_001, 40_500, "This call may be recorded."),
            u("in_hold", "AGENT", 10_500, 11_000, "This call may be recorded."),
        ]
        result = evaluate_rules(rows, context(call_duration_ms=45_000, holds=[{"start_ms": 10_000, "end_ms": 15_000, "tag": "HOLD"}]), self.ruleset)
        disclosure = next(item for item in result if item["rule_id"] == "opening_disclosure")
        self.assertEqual(disclosure["status"], "POTENTIAL_VIOLATION")
        self.assertEqual(disclosure["deadline_ms"], 35_000)

    def test_opening_deadline_sweeps_long_and_many_holds_once(self):
        self.assertEqual(_opening_deadline(0, 1, [(0, 500_000)]), 500_001)
        many_holds = [(index * 3, index * 3 + 2) for index in range(10_000)]
        self.assertEqual(_opening_deadline(0, 1_000_000, many_holds), 1_020_000)

    def test_each_ruleset_opportunity_threshold_controls_violation_timing(self):
        ten_second_rule = copy.deepcopy(self.ruleset)
        ten_second_rule["rules"][0]["opportunity_ms"] = 10_000
        complete_twelve_second_call = evaluate_rules([], context(call_duration_ms=12_000), ten_second_rule)
        self.assertEqual(next(item for item in complete_twelve_second_call if item["rule_id"] == "opening_disclosure")["status"], "POTENTIAL_VIOLATION")

        thirty_one_second_rule = copy.deepcopy(self.ruleset)
        thirty_one_second_rule["rules"][0]["opportunity_ms"] = 31_000
        # The agent connects 15 seconds into a complete 45 second call, so
        # only 30 seconds of eligible opportunity occurred.
        complete_call_below_threshold = evaluate_rules([], context(call_duration_ms=45_000, agent_connected_ms=15_000), thirty_one_second_rule)
        self.assertEqual(next(item for item in complete_call_below_threshold if item["rule_id"] == "opening_disclosure")["status"], "UNKNOWN")

    def test_unknown_role_crossing_opportunity_cutoff_makes_result_unknown(self):
        rows = [u("crossing", "UNKNOWN", 29_500, 30_500, "Unclear speaker.")]
        findings = evaluate_rules(rows, context(call_duration_ms=45_000), self.ruleset)
        disclosure = next(item for item in findings if item["rule_id"] == "opening_disclosure")
        self.assertEqual(disclosure["status"], "UNKNOWN")

    def test_unknown_role_crossing_either_closing_boundary_makes_result_unknown(self):
        for utterance in (
            u("cross-start", "UNKNOWN", 15_950, 16_050, "Unclear speaker."),
            u("cross-end", "UNKNOWN", 30_500, 31_500, "Unclear speaker."),
        ):
            with self.subTest(utterance=utterance.id):
                findings = evaluate_rules([utterance], context(call_duration_ms=31_000), self.ruleset)
                closing = next(item for item in findings if item["rule_id"] == "closing_farewell")
                self.assertEqual(closing["status"], "UNKNOWN")

    def test_hold_or_ivr_breaks_phrase_continuity(self):
        rows = [
            u("before", "AGENT", 9_000, 9_500, "This call may be"),
            u("ivr", "IVR", 10_000, 15_000, "Please wait while we connect you."),
            u("after", "AGENT", 16_000, 16_500, "recorded."),
        ]
        findings = evaluate_rules(
            rows,
            context(call_duration_ms=40_000, holds=[{"start_ms": 10_000, "end_ms": 15_000, "tag": "HOLD"}]),
            self.ruleset,
        )
        disclosure = next(item for item in findings if item["rule_id"] == "opening_disclosure")
        self.assertEqual(disclosure["status"], "POTENTIAL_VIOLATION")
        self.assertEqual(disclosure["evidence_ids"], [])

    def test_phrase_inside_extended_window_satisfies_and_unknown_role_abstains(self):
        rows = [u("good", "AGENT", 34_000, 34_500, "This call may be recorded.")]
        result = evaluate_rules(rows, context(call_duration_ms=45_000, holds=[{"start_ms": 10_000, "end_ms": 15_000, "tag": "HOLD"}]), self.ruleset)
        self.assertEqual(next(item for item in result if item["rule_id"] == "opening_disclosure")["status"], "SATISFIED")
        unknown = [u("unknown", "UNKNOWN", 1_000, 2_000, "This call may be recorded.")]
        result = evaluate_rules(unknown, context(), self.ruleset)
        disclosure = next(item for item in result if item["rule_id"] == "opening_disclosure")
        self.assertEqual(disclosure["status"], "UNKNOWN")

    def test_incomplete_audio_short_calls_and_independent_call_state(self):
        short = evaluate_rules([], context(call_duration_ms=10_000), self.ruleset)
        self.assertEqual(next(item for item in short if item["rule_id"] == "opening_disclosure")["status"], "UNKNOWN")
        incomplete = evaluate_rules([], context(call_duration_ms=40_000, complete=False), self.ruleset)
        self.assertEqual(next(item for item in incomplete if item["rule_id"] == "opening_disclosure")["status"], "UNKNOWN")
        satisfied = evaluate_rules([u("one", "AGENT", 1_000, 2_000, "This call may be recorded.")], context(call_id="call-one"), self.ruleset)
        missing = evaluate_rules([], context(call_id="call-two"), self.ruleset)
        self.assertEqual(next(item for item in satisfied if item["rule_id"] == "opening_disclosure")["status"], "SATISFIED")
        self.assertNotEqual(next(item for item in missing if item["rule_id"] == "opening_disclosure")["status"], "SATISFIED")

    def test_closing_check_runs_after_drain_and_sensitive_flags_never_keep_values(self):
        rows = [u("closing", "AGENT", 29_000, 30_000, "Thank you for your time.")]
        findings = evaluate_rules(rows, context(call_duration_ms=31_000), self.ruleset)
        self.assertEqual(next(item for item in findings if item["rule_id"] == "closing_farewell")["status"], "SATISFIED")
        sensitive = scan_sensitive_numbers([{"segment_id": "seg-1", "start_ms": 2_000, "end_ms": 4_000, "text": "My number is 415-555-0199."}], self.ruleset)
        self.assertEqual(len(sensitive), 1)
        self.assertEqual(sensitive[0]["rule_id"], "sensitive_number_advisory")
        self.assertNotIn("415-555-0199", json.dumps(sensitive))

    def test_sensitive_number_rule_uses_type_and_preserves_configured_id(self):
        renamed = copy.deepcopy(self.ruleset)
        renamed_rule = next(rule for rule in renamed["rules"] if rule["type"] == "SENSITIVE_NUMBER_ADVISORY")
        renamed_rule["id"] = "number_dlp"
        flag = scan_sensitive_numbers([{"segment_id": "segment-1", "start_ms": 2_000, "end_ms": 3_000, "text": "My number is 415-555-0199"}], renamed)
        self.assertEqual(flag[0]["rule_id"], "number_dlp")
        renamed["rules"].append(copy.deepcopy(renamed_rule))
        with self.assertRaises(RulesetError):
            compile_ruleset(renamed)
        optional = copy.deepcopy(self.ruleset)
        optional["rules"] = [rule for rule in optional["rules"] if rule["type"] != "SENSITIVE_NUMBER_ADVISORY"]
        self.assertEqual(len(compile_ruleset(optional)["rules"]), 2)
        self.assertEqual(scan_sensitive_numbers([{"segment_id": "segment-1", "text": "415-555-0199"}], optional), [])


@unittest.skipUnless(os.environ.get("DATABASE_URL") or os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class PolicyPersistenceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("DATABASE_URL") or not (urlparse(os.environ["DATABASE_URL"]).path or "").lstrip("/").endswith("_test"):
            raise RuntimeError("Refusing policy integration test without isolated DATABASE_URL ending in _test")
        cls.ruleset_raw = json.loads(Path(__file__).resolve().parent.parent.joinpath("config", "rules.v1.json").read_text(encoding="utf-8"))
        migrate()

    def test_versioned_findings_are_lease_scoped_upserted_and_tenant_isolated(self):
        from app.worker import make_policy_processor

        organisation_id, call_id, job_id = uuid4(), uuid4(), uuid4()
        other_organisation = uuid4()
        token = uuid4()

        def cleanup():
            with connect() as connection:
                connection.execute("DELETE FROM jobs WHERE organisation_id=%s AND call_id=%s", (organisation_id, call_id))
                connection.execute("DELETE FROM findings WHERE organisation_id=%s AND call_id=%s", (organisation_id, call_id))
                connection.execute("DELETE FROM transcript_utterances WHERE organisation_id=%s AND call_id=%s", (organisation_id, call_id))
                connection.execute("DELETE FROM audio_objects WHERE organisation_id=%s AND call_id=%s", (organisation_id, call_id))
                connection.execute("DELETE FROM events WHERE organisation_id=%s AND call_id=%s", (organisation_id, call_id))
                connection.execute("DELETE FROM calls WHERE organisation_id=%s AND id=%s", (organisation_id, call_id))
                connection.execute("DELETE FROM event_counters WHERE organisation_id=%s", (organisation_id,))
                connection.execute("DELETE FROM organisations WHERE id=ANY(%s)", ([organisation_id, other_organisation],))

        self.addCleanup(cleanup)
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s),(%s)", (organisation_id, other_organisation))
            connection.execute("INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state,transcript_revision) VALUES (%s,%s,%s,%s,%s,'agent','team','en','ANALYSING',1)", (organisation_id, call_id, f"policy-{call_id}", f"policy-{call_id}", "c" * 64))
            connection.execute("INSERT INTO audio_objects(organisation_id,id,call_id,private_key,checksum,codec,sample_rate,channels,duration_ms) VALUES (%s,%s,%s,%s,%s,'wav',16000,1,31000)", (organisation_id, uuid4(), call_id, f"policy-{call_id}.audio", "d" * 64))
            evidence_id = hashlib.sha256(b"seg-policy").hexdigest()[:32]
            connection.execute("INSERT INTO transcript_utterances(organisation_id,call_id,revision,id,segment_id,speaker_id,role,start_ms,end_ms,text_redacted,confidence,model_version,is_final) VALUES (%s,%s,1,%s,'seg-policy','channel-0','UNKNOWN',0,1000,'Uncertain role.',0.5,'synthetic-asr',true)", (organisation_id, call_id, evidence_id))
            connection.execute("INSERT INTO jobs(organisation_id,id,call_id,stage,input_revision,state,attempts,lease_token,lease_until) VALUES (%s,%s,%s,'POLICY',1,'RUNNING',1,%s,now()+interval '1 minute')", (organisation_id, job_id, call_id, token))
        job = {"id": str(job_id), "organisation_id": str(organisation_id), "call_id": str(call_id), "input_revision": 1}
        findings = make_policy_processor(self.ruleset_raw)(job)["findings"]
        self.assertEqual(next(item for item in findings if item["rule_id"] == "opening_disclosure")["status"], "UNKNOWN")
        with connect() as connection:
            self.assertTrue(finish_job(connection, str(job_id), str(token), {"findings": findings}))
            migration = connection.execute("SELECT 1 FROM schema_migrations WHERE version=7").fetchone()
            stored = connection.execute("SELECT status,ruleset_version,ruleset_hash,policy_text_version,evidence_ids,remediation FROM findings WHERE organisation_id=%s AND call_id=%s ORDER BY rule_id", (organisation_id, call_id)).fetchall()
            self.assertIsNotNone(migration)
            self.assertEqual(len(stored), 2)
            self.assertEqual(connection.execute("SELECT count(*) FROM findings WHERE organisation_id=%s", (other_organisation,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT processing_state FROM calls WHERE organisation_id=%s AND id=%s", (organisation_id, call_id)).fetchone()[0], "ANALYSING")
            updated = dict(findings[0]); updated["status"] = "POTENTIAL_VIOLATION"
            persist_policy_findings(connection, organisation_id, call_id, 1, [updated])
            self.assertEqual(connection.execute("SELECT count(*) FROM findings WHERE organisation_id=%s AND call_id=%s AND rule_id=%s", (organisation_id, call_id, updated["rule_id"])).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT status FROM findings WHERE organisation_id=%s AND call_id=%s AND rule_id=%s", (organisation_id, call_id, updated["rule_id"])).fetchone()[0], "POTENTIAL_VIOLATION")
            flag = scan_sensitive_numbers([{"segment_id": "seg-policy", "start_ms": 100, "end_ms": 900, "text": "Call 415-555-0199"}], self.ruleset_raw)[0]
            flag.update({"organisation_id": str(organisation_id), "call_id": str(call_id), "transcript_revision": 1})
            persist_policy_findings(connection, organisation_id, call_id, 1, [flag])
            sensitive_row = connection.execute("SELECT evidence_ids,remediation FROM findings WHERE organisation_id=%s AND call_id=%s AND rule_id='sensitive_number_advisory'", (organisation_id, call_id)).fetchone()
            self.assertEqual(sensitive_row[0], [evidence_id])
            self.assertNotIn("415-555-0199", str(sensitive_row))
            bad_scope = dict(flag, organisation_id=str(other_organisation))
            with self.assertRaises(ValueError):
                persist_policy_findings(connection, organisation_id, call_id, 1, [bad_scope])
            bad_evidence = dict(flag, evidence_ids=["not-in-this-transcript"])
            with self.assertRaises(ValueError):
                persist_policy_findings(connection, organisation_id, call_id, 1, [bad_evidence])


if __name__ == "__main__":
    unittest.main()
