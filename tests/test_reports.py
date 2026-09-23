"""Synthetic report scope, cohort, redaction, and export checks."""

import csv
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4

from app.auth import Scope
from app.db import connect
from app.migrate import migrate
from app.reports import ReportForbidden, ReportLimitError, ReportPrivacyUnavailable, csv_cell, export_findings, own_scores, team_report


class ReportUnitTests(unittest.TestCase):
    def test_formula_prefixes_are_neutralized_before_csv_quoting(self):
        for value in ["=1+1", "+cmd", "-1", "@SUM(A1)", " \t=1+1"]:
            with self.subTest(value=value):
                self.assertTrue(csv_cell(value).startswith("'"))
        self.assertEqual(csv_cell("Coaching note"), "Coaching note")
        output = io.StringIO(newline="")
        csv.writer(output).writerow((csv_cell('=cmd,"x"'),))
        self.assertEqual(next(csv.reader(io.StringIO(output.getvalue())))[0], "'=cmd,\"x\"")


def redact_synthetic(text: str) -> str:
    return text.replace("Alice Example", "[REDACTED]").replace("5550100199", "[REDACTED]")


@unittest.skipUnless(os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class ReportPersistenceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database = (urlparse(os.environ.get("DATABASE_URL", "")).path or "").lstrip("/")
        if not database.endswith("_test"):
            raise RuntimeError("Refusing report integration tests without isolated DATABASE_URL ending in _test")
        migrate()

    def create_dataset(self):
        organisation_id = uuid4()
        rubric_versions = ("rubric-v1", "rubric-v2")
        artifacts = ("model-a", "model-b")
        ids = []
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s)", (organisation_id,))
            for index in range(14):
                team = "team-a" if index < 10 else "team-b"
                agent = f"agent-{index}"
                call_id, audit_id, review_id = uuid4(), uuid4(), uuid4()
                created_at = datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(days=index)
                connection.execute(
                    "INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state,transcript_revision,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'en','NEEDS_REVIEW',1,%s)",
                    (organisation_id, call_id, str(call_id), str(call_id), "d" * 64, agent, team, created_at),
                )
                rubric_version = rubric_versions[(index % 10) // 5] if index < 10 else "rubric-small"
                artifact = artifacts[(index % 10) // 5] if index < 10 else "model-small"
                dimensions = [{"id": name, "status": "SCORED", "score": 3, "reason": "Evidence supports this score.", "evidence": []} for name in ("greeting", "listening", "resolution", "compliance", "clarity", "objection", "closing")]
                scores = {item["id"]: 3 for item in dimensions}
                threshold = "a" * 64
                connection.execute(
                    "INSERT INTO audits(organisation_id,id,call_id,revision,transcript_revision,model_artifact,inference_runtime,prompt_version,prompt_hash,rubric_version,rubric_hash,policy_provenance,policy_fingerprint,dimensions_json,overall_score,decision,coaching_narrative,highlights,improvement_areas,usage_json,attempts,latency_ms,pass_threshold) "
                    "VALUES (%s,%s,%s,1,1,%s,'local-v1','prompt-v1',%s,%s,%s,'[]'::jsonb,%s,%s::jsonb,3,'PASS',%s::jsonb,'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,1,10,3)",
                    (organisation_id, audit_id, call_id, artifact, "b" * 64, rubric_version, "c" * 64, threshold, json.dumps(dimensions), json.dumps({"text": "Coach Alice Example, call 5550100199.", "evidence": []})),
                )
                connection.execute(
                    "INSERT INTO reviews(organisation_id,id,audit_id,call_id,audit_revision,version,base_review_version,reviewer_id,action,changed_scores_json,effective_scores_json,effective_score,effective_decision,reason) "
                    "VALUES (%s,%s,%s,%s,1,1,0,'qa-reviewer','ACCEPT','{}'::jsonb,%s::jsonb,3,'PASS','Verified synthetic evidence')",
                    (organisation_id, review_id, audit_id, call_id, json.dumps(scores)),
                )
                rule_id = "=HYPERLINK(\"synthetic\")" if index == 0 else "disclosure_opening"
                connection.execute(
                    "INSERT INTO findings(organisation_id,id,call_id,transcript_revision,rule_id,ruleset_version,ruleset_hash,policy_text_version,status,severity,evidence_ids,evidence_fingerprint,deadline_ms,remediation) "
                    "VALUES (%s,%s,%s,1,%s,'rules-v1',%s,'policy-v1','UNKNOWN','MEDIUM','[]'::jsonb,%s,30000,%s)",
                    (organisation_id, uuid4(), call_id, rule_id, "e" * 64, "f" * 64 if index == 0 else (hex(index + 15)[2:] * 64)[:64], "Contact Alice Example at 5550100199. Review the approved procedure." if index == 0 else "Review the approved procedure."),
                )
                ids.append((str(call_id), agent, team, created_at))
        return organisation_id, ids

    def test_agent_report_is_own_only_redacted_and_separates_versions_and_scores(self):
        organisation_id, ids = self.create_dataset()
        scope = Scope(str(organisation_id), "agent-0", "AGENT", frozenset({"team-a"}))
        with connect() as connection:
            rows = own_scores(connection, scope, redact=redact_synthetic)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_id"], "agent-0")
        self.assertEqual(rows[0]["machine_score"], 3.0)
        self.assertEqual(rows[0]["reviewed_score"], 3.0)
        self.assertEqual(rows[0]["rubric_version"], "rubric-v1")
        self.assertNotIn("Alice Example", json.dumps(rows))
        self.assertNotIn("5550100199", json.dumps(rows))
        self.assertEqual(rows[0]["checklist"][0]["id"], "greeting")
        with connect() as connection:
            with self.assertRaises(ReportForbidden):
                own_scores(connection, Scope(str(organisation_id), "qa", "QA_ANALYST", frozenset()), redact=redact_synthetic)

    def test_team_report_uses_memberships_suppresses_small_teams_and_splits_versions(self):
        organisation_id, _ = self.create_dataset()
        start, end = datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)
        all_teams = Scope(str(organisation_id), "leader", "TEAM_LEADER", frozenset({"team-a", "team-b"}))
        with connect() as connection:
            report = team_report(connection, all_teams, start, end)
            limited = team_report(connection, Scope(str(organisation_id), "leader-a", "TEAM_LEADER", frozenset({"team-a"})), start, end)
            empty = team_report(connection, all_teams, datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertEqual(len(report["cohorts"]), 3)
        visible = [cohort for cohort in report["cohorts"] if not cohort["suppressed"]]
        self.assertEqual(len(visible), 2)
        self.assertEqual({cohort["model_artifact"] for cohort in visible}, {"model-a", "model-b"})
        self.assertTrue(all(cohort["distinct_agents"] == 5 for cohort in visible))
        small = next(cohort for cohort in report["cohorts"] if cohort["team_id"] == "team-b")
        self.assertTrue(small["suppressed"])
        self.assertIsNone(small["sample_count"])
        self.assertTrue(all(cohort["team_id"] == "team-a" for cohort in limited["cohorts"]))
        self.assertEqual(empty["cohorts"], [])
        with connect() as connection:
            with self.assertRaises(ReportForbidden):
                team_report(connection, Scope(str(organisation_id), "agent-0", "AGENT", frozenset({"team-a"})), start, end)

    def test_report_queries_never_cross_organisation_scope(self):
        organisation_id, _ = self.create_dataset()
        other_scope = Scope(str(uuid4()), "agent-0", "AGENT", frozenset({"team-a"}))
        start, end = datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)
        with connect() as connection:
            self.assertEqual(own_scores(connection, other_scope, redact=redact_synthetic), [])
            report = team_report(connection, Scope(other_scope.organisation_id, "leader", "TEAM_LEADER", frozenset({"team-a"})), start, end)
            content, count = export_findings(connection, Scope(other_scope.organisation_id, "compliance", "COMPLIANCE_OFFICER", frozenset()), start, end, redact=redact_synthetic)
        self.assertEqual(report["cohorts"], [])
        self.assertEqual(count, 0)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(content.decode("utf-8"))))), 0)

    def test_findings_export_is_bounded_redacted_logged_and_injection_safe(self):
        organisation_id, _ = self.create_dataset()
        scope = Scope(str(organisation_id), "compliance", "COMPLIANCE_OFFICER", frozenset())
        start, end = datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)
        with connect() as connection:
            with connection.transaction():
                content, row_count = export_findings(connection, scope, start, end, redact=redact_synthetic)
                event = connection.execute(
                    "INSERT INTO access_events(organisation_id,id,actor_id,action,resource_type,resource_id,outcome) "
                    "VALUES (%s,%s,%s,'FINDINGS_EXPORTED','FINDINGS_EXPORT',%s,'SUCCESS') RETURNING id",
                    (organisation_id, uuid4(), scope.user_id, uuid4()),
                ).fetchone()
        parsed = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
        self.assertEqual(row_count, 14)
        self.assertEqual(len(parsed), 14)
        self.assertTrue(parsed[0]["rule_id"].startswith("'"))
        self.assertNotIn("Alice Example", content.decode("utf-8"))
        self.assertNotIn("5550100199", content.decode("utf-8"))
        self.assertIn("[REDACTED]", parsed[0]["remediation"])
        self.assertIsNotNone(event)
        with connect() as connection:
            with self.assertRaises(Exception):
                with connection.transaction():
                    connection.execute("UPDATE access_events SET outcome='FAILURE' WHERE organisation_id=%s AND id=%s", (organisation_id, event[0]))
            with self.assertRaises(ReportForbidden):
                export_findings(connection, Scope(str(organisation_id), "leader", "TEAM_LEADER", frozenset({"team-a"})), start, end, redact=redact_synthetic)
            with self.assertRaises(ReportLimitError):
                export_findings(connection, scope, start, end + timedelta(days=100), redact=redact_synthetic)
            with self.assertRaises(ReportPrivacyUnavailable):
                export_findings(connection, scope, start, end, redact=lambda _text: (_ for _ in ()).throw(RuntimeError()))


if __name__ == "__main__":
    unittest.main()
