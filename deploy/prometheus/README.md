# Prometheus scrape config and alert rules (proposal, not deployed)

This directory is configuration for an operator to use if/when Prometheus is
actually deployed. Nothing in this repository runs Prometheus, Alertmanager,
or any scraper; these files are not wired into `compose.yaml`, the Dockerfile,
or CI. See `docs/operations-and-evaluation.md`'s "Metrics and alerting"
section for what is and is not implemented.

## Exporter

`GET /metrics` on the API process (`app/api.py`, rendered by
`app/metrics.py`) serves Prometheus text exposition format 0.0.4. It is
disabled (404) unless the `METRICS_TOKEN` environment variable is set to a
random value of at least 32 characters, and it requires
`Authorization: Bearer <METRICS_TOKEN>` on every scrape (401 otherwise). This
is a separate bearer secret from OIDC user auth -- generate and store it the
same way as any other deployment secret, and never commit a real value.

## Sample scrape config

```yaml
scrape_configs:
  - job_name: call-audit
    scheme: https
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/call-audit-metrics-token
    static_configs:
      - targets: ["call-audit-api.internal:8443"]
```

`credentials_file` (Prometheus's `bearer_token_file`) keeps the token out of
the scrape config file and out of Prometheus's own `/config` endpoint output;
mount it from the same secret store used for the other pinned deployment
secrets, readable only by the Prometheus process.

## Alert rules (`alerts.yml`)

Load this file as a Prometheus rule file (`rule_files:` in `prometheus.yml`).
Every `expr` in it references only a metric name `app/metrics.py` actually
emits -- `call_audit_database_up`, `call_audit_oldest_pending_job_age_seconds`
and `call_audit_worker_stage_attempts_total`. The retry-rate threshold (20%) is
stated in the rule file as a proposed default, not a measured value; re-tune it
once real traffic and false-positive rates are observed.

Workers only claim stages they have a configured handler for, so jobs for a
stage with no local model/ruleset yet stay `QUEUED` indefinitely by design. The
"falling behind" page is therefore restricted to stages with worker activity in
the last hour, and a separate warning (`CallAuditStageNotBeingServed`) fires for
a stage with old pending work and no attempts in 30 minutes. That warning cannot
tell "intentionally parked" from "every worker for this stage is down"; raise it
to critical once every stage you expect is configured.

**No Alertmanager receivers are configured anywhere in this repository, and
this repository sends no notifications of any kind.** These rules only ever
populate Prometheus's own `ALERTS`/`ALERTS_FOR_STATE` series and the
`/alerts` UI. Wiring an Alertmanager `route`/`receiver` (Slack, PagerDuty,
email, etc.) so that a firing alert actually notifies someone is a separate,
explicit operator decision, made outside this repository, per AGENTS.md's
instruction not to send messages to external recipients without
authorisation.

## What the operations guide's alert table is NOT covered here, and why

`docs/operations-and-evaluation.md`'s "Metrics and alerting" section lists a
broader set of signals than this exporter can currently back with a real
series:

| Ops-guide signal | Why it has no rule here |
|---|---|
| Redaction failure | Not tracked as a metric anywhere; redaction failure currently fails closed per-call (see `app/privacy.py`/worker processors) but is not counted or exported. |
| STT inference failure rate (>5% / 5 min) | `worker_stage_counters` records `RETRY_HANDLED`/`RETRY_HANDLER_FAILED`/`LEASE_LOST`/`COMMITTED` per *stage*, not per underlying failure cause, so a TRANSCRIBE-stage retry cannot currently be distinguished from any other stage failure inside that count. |
| Audit queue / GPU / compute spend trending over budget | No GPU, token, or cost telemetry is collected anywhere in this codebase. |
| Quality drift / override growth | No time-series of review-override rate is exported; `app/reports.py` only serves point-in-time, role-scoped aggregates over the API. |
| Tenant-isolation failure / raw-data leakage | This is a security-incident process, not a metric; no rule can safely proxy for it. |

Adding any of these requires actually instrumenting the corresponding code
path first (a real counter/gauge with a bounded label set), not just adding a
rule that references a metric name that does not exist.
