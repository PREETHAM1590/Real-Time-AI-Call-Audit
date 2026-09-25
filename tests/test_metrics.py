"""Aggregate metrics exporter checks: no identifiers, bounded labels, auth, DB-down path."""

import logging
import os
import re
import unittest
from urllib.parse import urlparse
from unittest.mock import patch
from uuid import uuid4

from app.metrics import (
    METRICS_TOKEN_MIN_LENGTH,
    metrics_token_from_env,
    record_stage_counter,
    render_database_down_text,
    render_metrics_text,
    verify_metrics_bearer,
)

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class _Result:
    def __init__(self, *, fetchall_value=None, fetchone_value=None):
        self._fetchall_value = fetchall_value
        self._fetchone_value = fetchone_value

    def fetchall(self):
        return list(self._fetchall_value or [])

    def fetchone(self):
        return self._fetchone_value


class FakeConnection:
    """Routes each query by keyword so tests do not depend on call order."""

    def __init__(self, *, job_rows=(), pending_rows=(), call_rows=(), stage_rows=(), session_rows=(), outbox_count=0, fail_on=None):
        self._job_rows, self._pending_rows, self._call_rows = job_rows, pending_rows, call_rows
        self._stage_rows, self._session_rows, self._outbox_count = stage_rows, session_rows, outbox_count
        self._fail_on = fail_on or ()

    def execute(self, sql, params=None):
        lowered = sql.lower()
        if "worker_stage_counters" in self._fail_on and "worker_stage_counters" in lowered:
            raise RuntimeError("synthetic failure")
        if lowered.startswith("select stage,state,count"):
            return _Result(fetchall_value=self._job_rows)
        if "min(created_at)" in lowered and "group by stage" in lowered:
            return _Result(fetchall_value=self._pending_rows)
        if "from calls" in lowered:
            return _Result(fetchall_value=self._call_rows)
        if "worker_stage_counters" in lowered:
            return _Result(fetchall_value=self._stage_rows)
        if "exotel_sessions" in lowered:
            return _Result(fetchall_value=self._session_rows)
        if "from events" in lowered:
            return _Result(fetchone_value=(self._outbox_count,))
        raise AssertionError(f"unexpected metrics query: {sql}")


class FailingConnection:
    def execute(self, *_args, **_kwargs):
        raise RuntimeError("synthetic connection failure")


class RenderMetricsTextTests(unittest.TestCase):
    def test_help_and_type_lines_present_for_every_series(self):
        text = render_metrics_text(FakeConnection())
        for name in (
            "call_audit_jobs", "call_audit_oldest_pending_job_age_seconds", "call_audit_calls",
            "call_audit_worker_stage_attempts_total", "call_audit_worker_stage_duration_milliseconds_total",
            "call_audit_live_sessions", "call_audit_event_outbox_events", "call_audit_database_up",
        ):
            self.assertIn(f"# HELP {name} ", text)
            self.assertIn(f"# TYPE {name} ", text)
        self.assertIn("call_audit_database_up 1", text)

    def test_bounded_labels_render_and_unexpected_values_map_to_unknown(self):
        connection = FakeConnection(
            job_rows=[("TRANSCRIBE", "QUEUED", 2), ("ROGUE_STAGE", "ROGUE_STATE", 3)],
            pending_rows=[("ANALYSE", 12), ("ROGUE_STAGE", 999)],
            call_rows=[("READY", 4), ("ROGUE_PROCESSING_STATE", 1)],
            stage_rows=[("AUDIT", "COMMITTED", 5, 500), ("ROGUE_STAGE", "ROGUE_OUTCOME", 1, 10)],
            session_rows=[("LIVE", 2), ("ROGUE_STATE", 1)],
            outbox_count=9,
        )
        text = render_metrics_text(connection)
        self.assertIn('call_audit_jobs{stage="TRANSCRIBE",state="QUEUED"} 2', text)
        self.assertIn('call_audit_jobs{stage="UNKNOWN",state="UNKNOWN"} 3', text)
        self.assertNotIn("ROGUE_STAGE", text)
        self.assertNotIn("ROGUE_STATE", text)
        self.assertNotIn("ROGUE_PROCESSING_STATE", text)
        self.assertNotIn("ROGUE_OUTCOME", text)
        self.assertIn('call_audit_oldest_pending_job_age_seconds{stage="UNKNOWN"} 999', text)
        self.assertIn('call_audit_calls{processing_state="UNKNOWN"} 1', text)
        self.assertIn('call_audit_worker_stage_attempts_total{stage="UNKNOWN",outcome="UNKNOWN"} 1', text)
        self.assertIn("call_audit_event_outbox_events 9", text)

    def test_two_rogue_rows_merging_into_unknown_are_summed_not_overwritten(self):
        connection = FakeConnection(
            job_rows=[("ROGUE_ONE", "QUEUED", 3), ("ROGUE_TWO", "QUEUED", 4)],
        )
        text = render_metrics_text(connection)
        self.assertIn('call_audit_jobs{stage="UNKNOWN",state="QUEUED"} 7', text)

    def test_oldest_pending_merge_takes_the_max_not_the_sum(self):
        connection = FakeConnection(pending_rows=[("ROGUE_ONE", 10), ("ROGUE_TWO", 200)])
        text = render_metrics_text(connection)
        self.assertIn('call_audit_oldest_pending_job_age_seconds{stage="UNKNOWN"} 200', text)

    def test_label_values_are_escaped_per_exposition_format(self):
        from app.metrics import _escape_label_value, _metric_line

        self.assertEqual(_escape_label_value('a"b\\c\nd'), 'a\\"b\\\\c\\nd')
        self.assertEqual(_metric_line("m", {"k": 'v"1'}, 3), 'm{k="v\\"1"} 3')
        self.assertEqual(_metric_line("m", {}, 1), "m 1")

    def test_render_contains_no_uuid_shaped_strings(self):
        connection = FakeConnection(
            job_rows=[("TRANSCRIBE", "QUEUED", 1)],
            stage_rows=[("AUDIT", "COMMITTED", 1, 20)],
            outbox_count=1,
        )
        text = render_metrics_text(connection)
        self.assertIsNone(_UUID_RE.search(text))

    def test_database_up_zero_and_no_other_series_when_query_fails(self):
        text = render_metrics_text(FailingConnection())
        self.assertEqual(text, render_database_down_text())
        self.assertIn("call_audit_database_up 0", text)
        self.assertNotIn("call_audit_jobs", text)

    def test_database_up_zero_when_only_a_later_query_fails(self):
        connection = FakeConnection(job_rows=[("TRANSCRIBE", "QUEUED", 1)], fail_on=("worker_stage_counters",))
        text = render_metrics_text(connection)
        self.assertEqual(text, render_database_down_text())

    def test_query_failure_logs_no_identifiers(self):
        with self.assertLogs("app.metrics", level=logging.WARNING) as logs:
            render_metrics_text(FailingConnection())
        self.assertEqual(len(logs.records), 1)
        self.assertNotIn("synthetic connection failure", "\n".join(logs.output))


class RecordStageCounterTests(unittest.TestCase):
    def test_swallows_connection_errors_and_logs_warning_without_identifiers(self):
        with patch("app.metrics.connect", side_effect=RuntimeError("no database configured")):
            with self.assertLogs("app.metrics", level=logging.WARNING) as logs:
                record_stage_counter("TRANSCRIBE", "COMMITTED", 12)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].event_name, "metrics.stage_counter_failed")
        self.assertNotIn("TRANSCRIBE", "\n".join(logs.output))

    def test_swallows_upsert_execution_errors(self):
        connection = FakingUpsertConnection()
        with patch("app.metrics.connect", return_value=connection):
            record_stage_counter("AUDIT", "COMMITTED", 5)
        self.assertTrue(connection.attempted)

    def test_caps_duration_added_per_attempt(self):
        connection = RecordingConnection()
        with patch("app.metrics.connect", return_value=connection):
            record_stage_counter("AUDIT", "COMMITTED", 999_999_999_999)
        self.assertEqual(connection.calls[0][1][2], 3_600_000)

    def test_negative_duration_is_clamped_to_zero(self):
        connection = RecordingConnection()
        with patch("app.metrics.connect", return_value=connection):
            record_stage_counter("AUDIT", "COMMITTED", -5)
        self.assertEqual(connection.calls[0][1][2], 0)


class FakingUpsertConnection:
    attempted = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, *_args, **_kwargs):
        FakingUpsertConnection.attempted = True
        raise RuntimeError("insert failed")


class RecordingConnection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params):
        self.calls.append((sql, params))


class MetricsTokenTests(unittest.TestCase):
    def test_disabled_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("METRICS_TOKEN", None)
            self.assertIsNone(metrics_token_from_env())

    def test_disabled_when_shorter_than_minimum(self):
        with patch.dict(os.environ, {"METRICS_TOKEN": "x" * (METRICS_TOKEN_MIN_LENGTH - 1)}):
            self.assertIsNone(metrics_token_from_env())

    def test_enabled_at_minimum_length(self):
        token = "synthetic-test-metrics-token-32c"
        self.assertGreaterEqual(len(token), METRICS_TOKEN_MIN_LENGTH)
        with patch.dict(os.environ, {"METRICS_TOKEN": token}):
            self.assertEqual(metrics_token_from_env(), token)

    def test_verify_bearer_rejects_missing_wrong_scheme_and_wrong_token(self):
        token = "synthetic-test-metrics-token-32c"
        self.assertFalse(verify_metrics_bearer(None, token))
        self.assertFalse(verify_metrics_bearer("", token))
        self.assertFalse(verify_metrics_bearer(f"Basic {token}", token))
        self.assertFalse(verify_metrics_bearer(f"Bearer wrong-{token}", token))
        self.assertFalse(verify_metrics_bearer("Bearer ", token))

    def test_verify_bearer_accepts_correct_token(self):
        token = "synthetic-test-metrics-token-32c"
        self.assertTrue(verify_metrics_bearer(f"Bearer {token}", token))


class MetricsEndpointTests(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from app.api import create_app
        from app.config import Settings

        settings = Settings(
            oidc_issuer="https://issuer.example.test",
            oidc_audience="call-audit-test",
            oidc_public_key="test-only-key",
            csrf_secret="test-only-csrf-secret-long-enough-for-settings",
            allowed_origins=("http://localhost:3000",),
        )
        app = create_app(settings, identity_lookup=lambda _subject: None)
        composed = app
        while not hasattr(composed, "openapi") and hasattr(composed, "app"):
            composed = composed.app
        self.fastapi_app = composed
        return TestClient(app)

    def test_returns_404_when_token_unset(self):
        client = self._client()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("METRICS_TOKEN", None)
            self.assertEqual(client.get("/metrics").status_code, 404)

    def test_returns_404_when_token_too_short(self):
        client = self._client()
        with patch.dict(os.environ, {"METRICS_TOKEN": "short-token"}):
            response = client.get("/metrics", headers={"Authorization": "Bearer short-token"})
        self.assertEqual(response.status_code, 404)

    def test_returns_401_on_missing_or_wrong_bearer(self):
        client = self._client()
        token = "synthetic-test-metrics-token-32characters"
        with patch.dict(os.environ, {"METRICS_TOKEN": token}):
            self.assertEqual(client.get("/metrics").status_code, 401)
            self.assertEqual(client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code, 401)
            self.assertEqual(client.get("/metrics", headers={"Authorization": token}).status_code, 401)

    def test_returns_200_with_correct_token_and_content_type_and_is_hidden_from_schema(self):
        client = self._client()
        token = "synthetic-test-metrics-token-32characters"
        with patch.dict(os.environ, {"METRICS_TOKEN": token}), patch("app.api.render_metrics_text", return_value="call_audit_database_up 1\n"), patch("app.api.connect"):
            response = client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/plain; version=0.0.4; charset=utf-8")
        self.assertIn("call_audit_database_up 1", response.text)
        self.assertNotIn("/metrics", self.fastapi_app.openapi()["paths"])

    def test_returns_200_with_database_down_text_when_connect_itself_fails(self):
        client = self._client()
        token = "synthetic-test-metrics-token-32characters"
        with patch.dict(os.environ, {"METRICS_TOKEN": token}), patch("app.api.connect", side_effect=RuntimeError("down")):
            response = client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("call_audit_database_up 0", response.text)


@unittest.skipUnless(os.environ.get("RUN_POSTGRES_INTEGRATION") == "1", "Set RUN_POSTGRES_INTEGRATION=1 to require PostgreSQL integration")
class MetricsPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database = (urlparse(os.environ.get("DATABASE_URL", "")).path or "").lstrip("/")
        if not database.endswith("_test"):
            raise RuntimeError("Refusing metrics integration tests without isolated DATABASE_URL ending in _test")
        from app.migrate import migrate

        migrate()

    def _counter_row(self, stage, outcome):
        from app.db import connect

        with connect() as connection:
            return connection.execute(
                "SELECT attempts,total_duration_ms FROM worker_stage_counters WHERE stage=%s AND outcome=%s",
                (stage, outcome),
            ).fetchone()

    def _restore_counter_row(self, stage, outcome, baseline):
        from app.db import connect

        with connect() as connection:
            if baseline is None:
                connection.execute("DELETE FROM worker_stage_counters WHERE stage=%s AND outcome=%s", (stage, outcome))
            else:
                connection.execute(
                    "UPDATE worker_stage_counters SET attempts=%s,total_duration_ms=%s WHERE stage=%s AND outcome=%s",
                    (baseline[0], baseline[1], stage, outcome),
                )

    def test_counter_upsert_increments_by_delta_not_absolute_value(self):
        stage, outcome = "AUDIT", "COMMITTED"
        baseline = self._counter_row(stage, outcome)
        self.addCleanup(self._restore_counter_row, stage, outcome, baseline)

        record_stage_counter(stage, outcome, 100)
        record_stage_counter(stage, outcome, 250)

        after = self._counter_row(stage, outcome)
        base_attempts, base_duration = baseline or (0, 0)
        self.assertEqual(after[0] - base_attempts, 2)
        self.assertEqual(after[1] - base_duration, 350)

    def test_metrics_text_reflects_two_tenants_without_leaking_org_id_and_stays_tenant_scoped(self):
        from app.db import connect
        from app.operations import operations_summary

        org_a, org_b = uuid4(), uuid4()
        call_a, call_b = uuid4(), uuid4()
        job_a, job_b = uuid4(), uuid4()
        with connect() as connection:
            connection.execute("INSERT INTO organisations(id) VALUES (%s),(%s)", (org_a, org_b))
            for organisation, call_id in ((org_a, call_a), (org_b, call_b)):
                connection.execute(
                    "INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state) "
                    "VALUES (%s,%s,%s,%s,%s,'agent','team','en','QUEUED')",
                    (organisation, call_id, str(call_id), str(call_id), "a" * 64),
                )
            connection.execute(
                "INSERT INTO jobs(organisation_id,id,call_id,stage,state) VALUES (%s,%s,%s,'TRANSCRIBE','QUEUED'),(%s,%s,%s,'AUDIT','QUEUED')",
                (org_a, job_a, call_a, org_b, job_b, call_b),
            )

        def cleanup():
            with connect() as connection:
                connection.execute("DELETE FROM jobs WHERE organisation_id=ANY(%s)", ([org_a, org_b],))
                connection.execute("DELETE FROM calls WHERE organisation_id=ANY(%s)", ([org_a, org_b],))
                connection.execute("DELETE FROM organisations WHERE id=ANY(%s)", ([org_a, org_b],))

        self.addCleanup(cleanup)

        with connect(timeout_seconds=5) as connection:
            text = render_metrics_text(connection)
        self.assertNotIn(str(org_a), text)
        self.assertNotIn(str(org_b), text)
        self.assertNotIn(str(call_a), text)
        self.assertNotIn(str(call_b), text)
        self.assertIsNone(_UUID_RE.search(text))

        with connect() as connection:
            summary_a = operations_summary(connection, str(org_a))
            summary_b = operations_summary(connection, str(org_b))
        self.assertEqual(summary_a["pending_by_stage"].get("TRANSCRIBE", {}).get("pending"), 1)
        self.assertIsNone(summary_a["pending_by_stage"].get("AUDIT"))
        self.assertEqual(summary_b["pending_by_stage"].get("AUDIT", {}).get("pending"), 1)
        self.assertEqual(summary_b["pending_by_stage"].get("TRANSCRIBE"), None)


class AlertRulesFileTests(unittest.TestCase):
    """The rule file must parse and every expr must reference a real emitted metric."""

    EMITTED_METRIC_NAMES = frozenset({
        "call_audit_jobs",
        "call_audit_oldest_pending_job_age_seconds",
        "call_audit_calls",
        "call_audit_worker_stage_attempts_total",
        "call_audit_worker_stage_duration_milliseconds_total",
        "call_audit_live_sessions",
        "call_audit_event_outbox_events",
        "call_audit_database_up",
    })

    def test_alert_rules_file_parses_and_only_references_emitted_metrics(self):
        import yaml
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "deploy" / "prometheus" / "alerts.yml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertIn("groups", document)
        rule_count = 0
        for group in document["groups"]:
            for rule in group["rules"]:
                self.assertIn("alert", rule)
                self.assertIn("expr", rule)
                referenced = set(re.findall(r"\bcall_audit_[a-z_]+\b", rule["expr"]))
                self.assertTrue(referenced, f"{rule['alert']} references no call_audit_ metric")
                self.assertTrue(
                    referenced.issubset(self.EMITTED_METRIC_NAMES),
                    f"{rule['alert']} references unknown metric(s): {referenced - self.EMITTED_METRIC_NAMES}",
                )
                rule_count += 1
        self.assertGreaterEqual(rule_count, 4)


if __name__ == "__main__":
    unittest.main()
