"""Synthetic integration checks for immutable human review revisions."""

import copy
import json
import os
import unittest
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from app.auth import Scope
from app.db import connect
from app.migrate import migrate
from tests.test_audit import FakeAdapter, good_response, policy_finding, utterance


@unittest.skipUnless(os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class ReviewPersistenceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database = (urlparse(os.environ.get("DATABASE_URL", "")).path or "").lstrip("/")
        if not database.endswith("_test"):
            raise RuntimeError("Refusing review integration tests without isolated DATABASE_URL ending in _test")
        migrate()

    def create_audit(self, *, role="AGENT", insufficient=False):
        from app.audit import audit_call, compile_rubric, persist_audit

        organisation_id, call_id = uuid4(), uuid4()
        rubric = json.loads(Path(__file__).resolve().parent.parent.joinpath("config", "rubric.v1.json").read_text(encoding="utf-8"))
        rubric["pass_threshold"] = 4.0
        response = good_response()
        if insufficient:
            for dimension in response["dimensions"]:
                dimension.update(status="INSUFFICIENT_EVIDENCE", score=None, evidence=[])
            response["coaching_narrative"] = {"text": "", "evidence": []}
            response["highlights"] = []
            response["improvement_areas"] = []
        result = audit_call(
            {"organisation_id": str(organisation_id), "call_id": str(call_id), "transcript_revision": 1},
            [utterance(role=role)], [policy_finding()], FakeAdapter(response), compile_rubric(rubric), "synthetic prompt",
        )
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s)", (organisation_id,))
            connection.execute("INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state,transcript_revision) VALUES (%s,%s,%s,%s,%s,'agent-a','team-a','en','NEEDS_REVIEW',1)", (organisation_id, call_id, f"review-{call_id}", f"review-{call_id}", "d" * 64))
            connection.execute("INSERT INTO transcript_utterances(organisation_id,call_id,revision,id,segment_id,speaker_id,role,start_ms,end_ms,text_redacted,confidence,model_version,is_final) VALUES (%s,%s,1,'u1','segment-1','channel-0',%s,0,1000,'I can help.',0.5,'synthetic-asr',true)", (organisation_id, call_id, role))
            persist_audit(connection, organisation_id, call_id, 1, result)
            audit_id = connection.execute("SELECT id FROM audits WHERE organisation_id=%s AND call_id=%s", (organisation_id, call_id)).fetchone()[0]
        return organisation_id, call_id, audit_id, result

    def test_review_revision_conflicts_are_atomic_and_machine_audit_stays_immutable(self):
        from app.reviews import ReviewConflict, append_review

        organisation_id, call_id, audit_id, machine = self.create_audit()
        qa = Scope(str(organisation_id), "qa-reviewer", "QA_ANALYST", frozenset())
        with connect() as connection:
            first = append_review(connection, qa, str(audit_id), 0, "ACCEPT", {}, "Verified synthetic evidence")
            self.assertEqual(first["version"], 1)
            self.assertEqual(first["effective_score"], 3.0)
            event = connection.execute("SELECT type,payload FROM events WHERE organisation_id=%s AND call_id=%s ORDER BY sequence DESC LIMIT 1", (organisation_id, call_id)).fetchone()
            self.assertEqual(event, ("call.updated", {"processing_state": "NEEDS_REVIEW", "transcript_revision": 1}))
            with self.assertRaises(ReviewConflict):
                append_review(connection, qa, str(audit_id), 0, "OVERRIDE", {"clarity": 2}, "Stale review")
            stored = connection.execute("SELECT overall_score,decision FROM audits WHERE organisation_id=%s AND id=%s", (organisation_id, audit_id)).fetchone()
            self.assertEqual(stored, (machine["overall_score"], machine["decision"]))
            self.assertEqual(connection.execute("SELECT count(*) FROM reviews WHERE organisation_id=%s AND audit_id=%s", (organisation_id, audit_id)).fetchone()[0], 1)

    def test_override_is_reasoned_tenant_scoped_and_server_scored(self):
        from app.reviews import ReviewForbidden, ReviewNotFound, append_review

        organisation_id, call_id, audit_id, machine = self.create_audit()
        qa = Scope(str(organisation_id), "qa-reviewer", "QA_ANALYST", frozenset())
        wrong_tenant = Scope(str(uuid4()), "qa-other", "QA_ANALYST", frozenset())
        agent = Scope(str(organisation_id), "agent-a", "AGENT", frozenset({"team-a"}))
        with connect() as connection:
            with self.assertRaises(ReviewNotFound):
                append_review(connection, wrong_tenant, str(audit_id), 0, "ACCEPT", {}, "No cross tenant review")
            with self.assertRaises(ReviewForbidden):
                append_review(connection, agent, str(audit_id), 0, "ACCEPT", {}, "Agent cannot review")
            with self.assertRaises(ValueError):
                append_review(connection, qa, str(audit_id), 0, "OVERRIDE", {"clarity": 2}, "  ")
            result = append_review(connection, qa, str(audit_id), 0, "OVERRIDE", {"resolution": 5}, "Evidence supports a higher resolution score")
            self.assertEqual(result["effective_score"], 3.5)
            self.assertEqual(result["effective_decision"], "FAIL")
            self.assertEqual(result["effective_scores"]["resolution"], 5)
            second = append_review(connection, qa, str(audit_id), 1, "OVERRIDE", {"clarity": 2}, "Evidence supports a lower clarity score")
            self.assertEqual(second["effective_scores"]["resolution"], 5)
            self.assertEqual(second["effective_scores"]["clarity"], 2)
            self.assertEqual(second["effective_score"], 3.4)
            self.assertEqual(connection.execute("SELECT overall_score FROM audits WHERE organisation_id=%s AND id=%s", (organisation_id, audit_id)).fetchone()[0], machine["overall_score"])
            events = connection.execute("SELECT payload FROM events WHERE organisation_id=%s AND call_id=%s ORDER BY sequence", (organisation_id, call_id)).fetchall()
            self.assertEqual([event[0] for event in events], [{"processing_state": "NEEDS_REVIEW", "transcript_revision": 1}] * 2)

    def test_nondefault_rubric_threshold_is_persisted_for_review_recomputation(self):
        organisation_id, _, audit_id, _ = self.create_audit()
        with connect() as connection:
            threshold = connection.execute("SELECT pass_threshold FROM audits WHERE organisation_id=%s AND id=%s", (organisation_id, audit_id)).fetchone()[0]
        self.assertEqual(float(threshold), 4.0)

    def test_needs_review_queue_item_can_be_triaged_without_becoming_scored(self):
        from app.reviews import ReviewConflict, append_review, review_queue

        organisation_id, call_id, audit_id, machine = self.create_audit(role="CUSTOMER", insufficient=True)
        qa = Scope(str(organisation_id), "qa-reviewer", "QA_ANALYST", frozenset())
        self.assertEqual(machine["decision"], "NEEDS_REVIEW")
        self.assertIsNone(machine["overall_score"])
        with connect() as connection:
            result = append_review(connection, qa, str(audit_id), 0, "TRIAGE", {}, "No agent speech is available to score")
            queued = review_queue(connection, qa)
            with self.assertRaises(ReviewConflict):
                append_review(connection, qa, str(audit_id), 1, "TRIAGE", {}, "Duplicate acknowledgement")
            event = connection.execute("SELECT type,payload FROM events WHERE organisation_id=%s AND call_id=%s ORDER BY sequence DESC LIMIT 1", (organisation_id, call_id)).fetchone()
            self.assertEqual(event, ("call.updated", {"processing_state": "NEEDS_REVIEW", "transcript_revision": 1}))
        self.assertEqual(result["action"], "TRIAGE")
        self.assertIsNone(result["effective_score"])
        self.assertEqual(result["effective_decision"], "NEEDS_REVIEW")
        self.assertIn(str(call_id), {item["call_id"] for item in queued})

    def test_superseded_transcript_cannot_receive_a_score_action(self):
        from app.reviews import ReviewConflict, append_review

        organisation_id, call_id, audit_id, _ = self.create_audit()
        qa = Scope(str(organisation_id), "qa-reviewer", "QA_ANALYST", frozenset())
        with connect() as connection:
            connection.execute("UPDATE calls SET transcript_revision=2 WHERE organisation_id=%s AND id=%s", (organisation_id, call_id))
            with self.assertRaises(ReviewConflict):
                append_review(connection, qa, str(audit_id), 0, "ACCEPT", {}, "Old transcript")

    def test_call_detail_does_not_pair_old_disposition_with_new_transcript(self):
        from app.reviews import call_detail

        organisation_id, call_id, _, _ = self.create_audit()
        qa = Scope(str(organisation_id), "qa-reviewer", "QA_ANALYST", frozenset())
        with connect() as connection:
            content_hash = "c" * 64
            connection.execute("INSERT INTO disposition_config_versions(organisation_id,config_id,use_case_id,version,content_hash,schema_version,raw_json,created_by) VALUES (%s,'synthetic_cfg','general',1,%s,'1.0.0','{}'::jsonb,'admin')", (organisation_id, content_hash))
            connection.execute("INSERT INTO dispositions(organisation_id,id,call_id,revision,transcript_revision,config_id,config_version,config_hash,schema_version,model_artifact,adapter_version,processing_path,status,code,parent_code,confidence,requires_review,review_reason,matched_rule_id,signals_json,usage_json) VALUES (%s,%s,%s,1,1,'synthetic_cfg',1,%s,'1.0.0','sha256:synthetic','synthetic-adapter','ABSTAIN','NEEDS_REVIEW',NULL,NULL,NULL,true,'UNKNOWN_SIGNAL',NULL,'{}'::jsonb,'{}'::jsonb)", (organisation_id, uuid4(), call_id, content_hash))
            connection.execute("UPDATE calls SET transcript_revision=2 WHERE organisation_id=%s AND id=%s", (organisation_id, call_id))
            detail = call_detail(connection, qa, str(call_id))
        self.assertEqual(detail["call"]["transcript_revision"], 2)
        self.assertIsNone(detail["disposition"])
