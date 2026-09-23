# Operations, evaluation and rollout

Status: implementation is in progress; nothing is deployed. The locked Python runtime, API and worker entrypoints, and non-root backend Docker image exist. Local PostgreSQL is the only Compose service; there is no API/worker deployment stack or CI workflow. Model artifacts, identity secrets and database credentials remain operator-provided inputs.

## Environments and configuration

Use local synthetic data, isolated staging with approved samples, and production with separate identity clients, keys, model artifacts, databases and buckets. Never copy production audio to developer machines by default. Model inference runs on project-controlled hardware or a dedicated private deployment under project control.

Current upload intake accepts a verified OIDC subject mapped to one active server-side AGENT membership with one team in `identity_memberships`. An optional organisation selector only chooses among database-verified memberships. It does not trust submitted agent/team fields. Cookie-authenticated mutations require the exact allowlisted Origin and a CSRF token from `GET /v1/csrf`; provision a non-placeholder `CSRF_SECRET`. QA/admin and telephony/batch upload remain unavailable until a trusted server-side source-to-agent mapping is implemented and tested.

The analyst page at `/analyst` includes a manual upload form for WAV/MP3 recordings. Uploads require an AGENT account with exactly one server-resolved team; QA and administrator accounts cannot use this form. Enter the external reference and a language tag of at most 32 characters. The page accepts `.wav` and `.mp3` filenames and blocks files over 250 MiB before sending them; backend media validation remains authoritative. It posts multipart data to the existing `POST /v1/calls` endpoint with the CSRF token and an `Idempotency-Key`. If a network failure leaves the result uncertain, retry without changing the form to reuse the same key; editing any field or a successful upload starts a new key. An already-used external reference returns 409 and asks for a different reference, including when concurrent requests race. Reusing a key with a different reference, language or audio returns a distinct 409; retry the original unchanged upload. While an upload is in flight, every form control is disabled to prevent edits from being discarded by a successful reset. Use synthetic recordings until real-data processing and retention policies are approved.

Current settings: `DATABASE_URL`, `AUDIO_STORAGE_PATH`, `FFPROBE`, `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_PUBLIC_KEY`, `CSRF_SECRET`, and `ALLOWED_ORIGINS`. The transcription adapter additionally requires `FASTER_WHISPER_MODEL_PATH`, `FASTER_WHISPER_MODEL_SHA256`, and `FASTER_WHISPER_MODEL_VERSION`; set `FASTER_WHISPER_DEVICE` and `FASTER_WHISPER_COMPUTE_TYPE` for the selected hardware. Redaction requires `PRESIDIO_SPACY_MODEL_PATH` and `PRESIDIO_SPACY_MODEL_SHA256`. The example identity key, secret, database password and origin are placeholders; provision environment-specific values before starting the service. The secret must be at least 32 bytes and cannot be the placeholder. Planned additions include bounded stage concurrency and approved retention settings. Disposition currently uses `DISPOSITION_MODEL_PATH`, `DISPOSITION_MODEL_SHA256`, `DISPOSITION_LOCAL_URL`, and `DISPOSITION_LOCAL_MODEL_NAME` as detailed below; object-bucket and generic local-LLM settings are not current settings.

Production must fail startup when identity, encryption/storage, retention or pinned local model paths/checksums are missing. Set per-model concurrency, GPU memory and queue limits; reject or defer new work visibly when capacity is exhausted. Inference services must not download weights or send call data to external AI endpoints at runtime. The current decoder has size, duration, timeout and concurrency bounds but lacks OS-enforced CPU and memory limits; this blocks all real-data and production use until isolated-process limits and a resource-exhaustion test are in place. Policy evaluation is available through a locally pinned ruleset: set both `POLICY_RULESET_PATH` (absolute path) and `POLICY_RULESET_SHA256`; an incomplete pair fails worker startup, and without a configured ruleset the POLICY job remains parked. Current uploads do not provide trusted agent-connection, hold, completion or call-type context, so the stored safe defaults leave timed findings `UNKNOWN`. Integrate and qualify an authenticated telephony context source before relying on opening/closing policy results; never infer reliable timing from uploaded audio or caller-supplied fields.

Local developer workflow:

The committed `uv.lock` pins the resolved Python dependencies. Copy `.env.example` to `.env`, then replace all example identity keys/secrets and set an allowed local origin before starting the API. These are local development instructions; do not use the Compose database for integration tests. Integration tests require a separate disposable database whose name ends in `_test`.

```powershell
uv sync --extra test
docker compose up -d postgres
uv run python -m app.migrate
# For integration tests, point DATABASE_URL at a separate database ending in _test.
uv run python -m unittest discover -s tests -v
uv run uvicorn app.main:app --reload
# Separate terminals:
uv run python -m app.worker
```

The worker polls with an idle interval from 0.5 to 10 seconds (`WORKER_IDLE_POLL_SECONDS`, default 1 second), handles SIGINT/SIGTERM, and leaves stages parked when their local model/ruleset handler is not configured. No model artifacts are downloaded at startup. `compose.yaml` contains only local PostgreSQL and its fixed `local-development-only` password is strictly for disposable development; never reuse it outside that context.

The Dockerfile builds one backend image with the locked base, transcription and privacy dependencies, includes `ffmpeg`, and runs as UID/GID 10001. It contains no model weights or secrets. Build it locally with `docker build -t call-audit:local .`; provide validated identity settings, database URL, storage mount and pinned local artifacts at runtime. The image's default command starts the API; run the worker from the same image with `docker run ... call-audit:local python -m app.worker`. This repository does not yet define Compose API/worker/migration services or CI, and a successful image build is packaging evidence only—not deployment, model readiness, or production qualification. The browser page is served by FastAPI; no npm build/dev command applies.

## Release validation

| Layer | Inputs | Required evidence |
|---|---|---|
| Domain | Synthetic text and segment fixtures | Disclosure timing, unknown roles, silent calls, split phrases, duplicate segments, exact evidence and scoring checks |
| Disposition | Synthetic redacted final utterances, authoritative facts, versioned config fixtures | Per-code precision/recall and confusion, abstention/review rate, calibration/coverage, front-gate authority, rule trace completeness, replay/reproducibility, tenant isolation and config rollback |
| Integration | PostgreSQL, private storage and local inference fakes | Duplicate upload, expired lease, mid-stage crash, review conflict, deletion race and tenant isolation |
| Local model contract | Approved short recordings and pinned model artifacts | Segment finalisation, timestamps, language, model refusal/error mapping, output validation and resource use |
| Browser | Analyst, supervisor, agent and forbidden-user sessions | Accessible queue/review, permitted playback, own-score scope, stale feed and resumption |
| Quality | Human-labelled evaluation set | Per-rule recall/precision, rubric agreement, role accuracy, WER, sentiment calibration and subgroup breakdown |
| Load/recovery | Synthetic recordings at pilot volume | Latency percentiles, memory bounds, queue drain, restart recovery and restore drill |

Use deterministic local inference fakes in ordinary CI. Run actual pinned-model tests in isolated staging with the selected CPU/GPU allocation. Fail visibly if an artifact or runtime is missing; never silently substitute fake results. Verify inference still works with outbound internet blocked after controlled provisioning.

## Golden-set protocol

Start with 100 adjudicated calls across normal, high-risk, silent, short, noisy, overlapping, mono, dual-channel and prompt-injection cases. Keep identities and recordings restricted; use redacted transcript fixtures in source control. Separate prompt-development examples from held-out evaluation examples. Deduplicate by underlying conversation so train/evaluation versions cannot overlap.

Two QA reviewers independently score the initial set and adjudicate disagreements. Record rubric/ruleset version, evidence, applicable dimensions and uncertain labels. Track inter-reviewer agreement before treating a human label as ground truth.

Proposed pilot gates:

- Mean absolute overall-score error ≤0.3 on the 1–5 scale for evaluable calls; report sample count and uncertainty.
- Decision agreement ≥92% on held-out evaluable calls; separately report review/abstention rate and coverage.
- No missed seeded critical case in deterministic regression fixtures; human-labelled critical recall ≥98% where sample size permits meaningful reporting.
- Unsupported or invented evidence accepted by the server: zero in the adversarial suite.
- No cross-tenant disclosure in authorisation tests; zero known raw-PII leakage in the privacy fixture suite.
- Report results separately for language/accent, channel configuration, noise and duration. Insufficient subgroup samples block claims about that subgroup.

These are proposed release thresholds, not current measurements. A 100-call set alone cannot establish a reliable rare-event false-negative rate; gather enough positive cases and report confidence intervals before making that claim. Keep all excluded and abstained cases in the report.

For every prompt/model/rule/config change: run deterministic checks → held-out evaluation (including per-disposition confusion and review coverage) → shadow run → QA sample review → versioned promotion. Retain old versions and results. Do not rewrite old audits/dispositions to make a new model appear consistent. A secondary model is an optional sampled calibration tool once a measured benefit justifies its cost.

## Metrics and alerting

Collect call intake rate, audio gaps, STT errors/time-to-final, job age, retries/dead letters, stage durations, incomplete calls, redaction failures, invalid evidence, LLM refusal/schema failure, tokens, GPU memory/utilisation, model load failures, queue wait, SSE disconnects, export volume and review overrides. Do not attach raw text or high-cardinality caller identifiers to metric labels.

### Current observability slice

The worker emits one structured `worker.stage_outcome` log record for each claimed attempt, with only allowlisted stage, bounded attempt number, outcome and elapsed milliseconds. It does not log job/call/tenant identifiers, exception text, transcript text, or model request/response bodies. `GET /health` is a process liveness check. `GET /ready` checks `SELECT 1` and returns database availability with `model_readiness: unknown`; it does not probe or claim local model readiness. `GET /v1/operations/summary` is ADMIN-only and tenant-scoped; it returns pending job count, oldest pending age capped at 365 days, and calls not in READY, with no per-call rows or identifiers. The incomplete count reflects pipeline state (and includes failed/needs-review records), not trusted telephony call completeness.

These are request/log-level checks, not a metrics exporter, time-series store, dashboard, alerting system or spend measurement. Model readiness, STT/LLM capacity, GPU usage, tokens and compute cost remain unknown/unmeasured until deployment instrumentation and selected local models are configured.

Separate latency clocks: call end, object available, job enqueued, local inference request, final received, audit committed and browser rendered. For live calls also measure audio arrival to stable final; windowed Whisper may dominate this interval. Use monotonic durations within a process and synchronised UTC timestamps across services; include clock-skew monitoring.

| Signal | Initial response |
|---|---|
| Any tenant-isolation failure or raw-data leakage | Security incident; disable affected access path, preserve restricted evidence and follow organisation response process |
| Processing unavailable or oldest eligible job >5 minutes for 5 minutes | Page on-call; inspect dependencies and lease recovery |
| STT inference failures >5% over 5 minutes, minimum 100 windows | Defer intake/limit concurrency; expose degraded state and inspect model worker |
| Redaction failure | Fail closed for affected call; alert on sustained failures, never bypass redaction |
| Audit queue or GPU/compute spend trending over budget | Business-hours capacity alert; cap submissions without dropping jobs |
| Quality drift / override growth | QA investigation; compare matched versions and case mix before attributing model failure |

Alert notifications are proposed integration work; this documentation does not create or send any notifications.

### Exotel AgentStream intake status

The API now exposes ADMIN-only tenant-scoped integration and agent-mapping routes under `/v1/exotel-integrations`, plus the Basic-authenticated WSS endpoint `/v1/exotel/stream`. Integration creation returns a generated username/password once; record the password in the approved secret store and configure it in the Exotel WSS URL, because later list responses omit credentials. Map each Exotel `start.custom_parameters.agent_ref` to an active AGENT identity with exactly one active team membership before accepting calls. The path accepts only a complete connected/start/media/stop lifecycle with contiguous sequence, chunk and timestamp positions, bounded 8 kHz mono PCM, and submits the WAV through ordinary durable intake after stop. A gap, disconnect, stale generation, invalid mapping or disabled integration fails closed.

This is code-path evidence from synthetic WebSocket tests, not an Exotel tenant test. Account entitlement, actual callflow configuration, media direction/coverage and production WSS networking remain unverified. No live STT, supervisor monitoring, two-leg attribution, capacity qualification, vendor operations or real-call processing is established; use synthetic traffic until real-data and retention approvals are in place.

## Failure and recovery runbook

| Incident | Action | Recovery evidence |
|---|---|---|
| Local STT worker unavailable | Leave jobs retryable, honour retry budgets, retain approved audio, show delayed state | Model worker restored; replay completes once without duplicate utterances |
| LLM timeout/refusal/invalid evidence | Retry only within budget; then NEEDS_REVIEW | Analyst can still inspect transcript and policy findings |
| Worker crash | Let lease expire; new worker resumes from committed stage | Same input revision produces one effective audit |
| Media disconnect | Mark gap, drain final results, close generation after timeout | Incomplete call never receives confident all-clear; approved recording can create corrected revision |
| SSE cursor expired | Send reset_required; client reloads scoped snapshot | No missing persisted finding and no duplicate alert |
| Wrong prompt/model release | Stop promotion and select previous version for new work | Existing records retain original versions; selective re-audit creates new revisions |
| Database outage | Reject new upload finalisation; preserve staged objects temporarily | Orphan cleanup and resumable intake reconcile objects and records |
| Accidental deletion / restore | Restore into isolated environment, reapply tombstones before exposing data | Restricted operator verifies deleted content stays inaccessible |

Proposed pilot RPO: ≤24 hours using daily encrypted backups. Proposed RTO: ≤4 hours, established by a timed restore exercise. Upgrade these targets before a deployment requiring tighter recovery; do not describe daily backups as zero-data-loss.

## Retention/deletion checklist

1. Authorise and append deletion request; check recorded hold status.
2. Tombstone call; deny APIs/playback and cancel queued work.
3. Workers check tombstone before inference requests and before commit.
4. Delete audio, transient objects, transcript, findings and jobs only after the tombstone is committed. Current `purge_call` scrubs mutable call identity fields and removes bounded private audio objects and those mutable derivatives.
5. **Current restriction:** machine audits, disposition revisions, human reviews and access events are immutable and remain in PostgreSQL after this content purge. `purge_call` returns `PARTIAL_IMMUTABLE_HISTORY` with per-table counts when they remain; `access_events` includes direct CALL events, review events linked through the call's audit IDs, audio grants linked through its audio-object IDs, and the purge event written in the same transaction. Even a call without audits retains deletion/purge and any audio-access history and is therefore reported as partial, not fully purged. Redacted evidence/reason fields can still identify a person or call. This is not full deletion or verified erasure.
6. Production retention and purge are blocked until the privacy/policy owner approves immutable-history retention or an independently verifiable crypto-erasure design, including exports, backups and restored copies. Do not bypass immutability triggers or report content as fully erased.
7. The ADMIN `DELETE /v1/calls/{id}` request commits the tombstone and access event, marks the call DELETING, and cancels queued work before returning 202. Expiry uses `retention_expires_at` and `legal_hold`; no default expiry is assigned. `tombstone_expired_calls` is bounded to 100 rows per call and supports an organisation scope for controlled operation. These library operations are not scheduled, exposed as a purge API, or deployed as a background service.
8. Synthetic PostgreSQL tests exercise hold, future expiry, exact expiry, repeated purge, immutable-history preservation and a worker finish racing a tombstone. They do not verify object-store versions, replicas, model caches, backup expiry or restore-time tombstone replay.
9. Before serving restored data, reapply deletion records and verify all relevant storage layers under the approved policy.

Do not claim immediate deletion from immutable backups; document their expiry and restore restrictions. Retention tests use an injectable clock, never wait days in CI. No retention duration, legal-hold mutation workflow, purge scheduler, restore drill, or verified-erasure claim is currently approved or implemented.

## Current synthetic evaluation command

The evaluator accepts bounded JSONL records containing only pseudonymous case IDs, adjudicated finding labels, prediction statuses and dimension scores. It rejects transcript/raw-text fields. It writes a deterministic JSON report with dataset SHA-256, sample/exclusion/abstention counts, coverage, per-rule precision/recall, critical misses and score agreement. Undefined precision/recall and score-agreement ratios are `null`, not zero or perfect. `tests/fixtures/golden.jsonl` is synthetic test data, not an estimate of model quality.

```powershell
python -m app.evaluate --dataset tests/fixtures/golden.jsonl --output evaluation-report.json
```

This local report is a tool check only. There is no approved human-labelled quality dataset, pinned production model, measured quality result, restore/load result, or production readiness claim.

## Capacity and cost model

Do not reuse the article's bill as a quote. Its STT table labels a rate per hour that is later used per minute, and its storage calculation mixes accumulated GB with daily intake.

```text
audio_minutes_day = calls_day * average_minutes
average_concurrency = audio_minutes_day / operating_minutes_day
gpu_hours_day = sum(gpu_count_by_pool * active_hours_by_pool)
inference_compute_cost_day = gpu_hours_day * gpu_hourly_cost
cpu_compute_cost_day = cpu_hours_day * cpu_hourly_cost
retained_audio_GB = audio_GB_day * retention_days
storage_cost_month = retained_audio_GB * contracted_price_per_GB_month
cost_per_completed_audit = total_period_cost / completed_audits_period
```

Track model download/storage, GPU idle time, CPU, electricity or cloud compute, retries, storage requests, egress, database, observability, backups and human review costs. Audits that fail still consume capacity; include them in total cost. Measure calls/hour/GPU and tokens/second at target concurrency rather than multiplying hosted API token prices.

At the article's illustrative 50,000 calls/day, five minutes each and an eight-hour window, average concurrency is about 521 calls. A peak of 800 is therefore plausible but not sufficient headroom by itself. Uncompressed 16 kHz/16-bit mono is 32,000 bytes/second: 250,000 minutes is 480 GB/day (decimal), or 960 GB/day stereo. Actual stored codecs change this materially. Ninety-day retention accumulates many days of storage, not one day's volume.

Before choosing production infrastructure, measure peak/average ratio, local STT real-time factor, LLM tokens/second, GPU memory, worker service time, database growth, dashboard fan-out, export volume and review demand. If there are 50,000 calls/day and even 5% require review, that is 2,500 reviews/day; staffing capacity and prioritisation must be part of rollout.

## Rollout and ownership

Indicative effort for two engineers plus part-time QA/policy support: 2–3 weeks for post-call core, 1–2 weeks for analyst workflow, 2–3 weeks for live integration, and 1–2 weeks for operations and portal. Add explicit time for GPU provisioning, artifact/license review and local-model benchmarking; allow at least six weeks of overlapping analyst calibration. These are planning ranges, not commitments.

Engineering owns correctness, recovery and interfaces. QA owns adjudication and coaching usefulness. The policy/privacy owner owns rule content, recording policy, retention, telephony processing and model-license approval. Operations owns model provisioning, deployment, incident response and restore exercises.

Release sequence: synthetic demo → shadow post-call pilot → QA-approved limited team → live shadow mode → live advisory alerts → production gate. No automatic disciplinary decisions or external coaching messages are introduced by the pilot. Coaching notes are saved in the application.

Before production, record: policy/retention decisions; exact model/code licenses, revisions and checksums; target hardware and offline-inference proof; golden-set report; security and accessibility results; restore and deletion evidence; live load report; compute cost projection; named on-call and rollback owner.

## Disposition implementation boundary

The current disposition adapter is a local OpenAI-compatible JSON client intended for a colocated vLLM service. Deployment must set `DISPOSITION_MODEL_PATH` to an absolute, read-only model directory mounted at the same path in the worker/API and model-serving runtimes, `DISPOSITION_MODEL_SHA256` to that directory's verified recursive digest, `DISPOSITION_LOCAL_URL` to the loopback `/v1` endpoint, and `DISPOSITION_LOCAL_MODEL_NAME` to the model ID served there. Startup verifies the local directory contents; each inference request requires the loopback `/v1/models` manifest to report both the configured model ID and a `root` resolving to the exact verified directory. A matching alias with a different or absent root fails closed. Operators must provision the server out of band from this pinned artifact. The application does not download models or fall back to hosted inference. A missing digest leaves `ANALYSE` without a handler and jobs parked; an invalid configured artifact path fails worker startup; a server manifest mismatch fails inference and follows bounded retry/failure handling.

Disposition config activation requires an explicit immutable approval event from an ADMIN whose identity differs from the version creator. Activation is the pilot's deployment action after that independent approval; rollback is limited to previously active, approved versions and records a reason. `status` and `approved_by` are derived from append-only events rather than mutable version-row columns.

Disposition windows use character ceilings as a conservative implementation bound, not a measured token budget: each request is at most 100,000 characters, full-call input at most 2,000,000 characters, and each job at most 128 windows. Adjacent utterances from the same known speaker are grouped first and never split across windows; evidence still refers to the canonical member utterance IDs. The default one-active-config-per-organisation pointer is an initial simplifying limit; `config_id` and `use_case_id` do not confer tenant authority. Per-use-case activation requires an authoritative call-routing key and membership policy. `ANALYSE` currently ends with a disposition result and `NEEDS_REVIEW`; it does not mean policy evaluation or QA scoring completed and cannot set `READY`. No model quality, calibration, latency, production capacity or deployment readiness is claimed by this code slice.

The post-call `AUDIT` worker stores a separate immutable seven-dimension result. The database rejects audit updates and deletes; the uniqueness identity includes model artifact, inference runtime, prompt version and hash, rubric hash, policy fingerprint and transcript revision so a new implementation version is retained. It accepts only exact quotes from final redacted AGENT utterances, rejects unsupported or malformed model output, and computes weighted scores and decisions in application code. Customer-only transcripts abstain with no score or coaching claim. Inference is local-only: deployment must provide an absolute pinned model directory and checksum, a private loopback OpenAI-compatible endpoint, and a `/v1/models` manifest whose reported model ID and root match that verified artifact. Missing or mismatched configuration fails closed; there is no runtime model download or hosted fallback. The request bound is currently a conservative 100,000 characters, not a tokenizer-measured token limit. Tests use deterministic synthetic adapters. No production audit model has been approved or provisioned, and no golden-set quality, calibration, throughput, latency or hardware-capacity evidence has been collected. The audit result leaves calls in `NEEDS_REVIEW`; Task 6 analyst review and trusted telephony timing remain release gates, so no result from these stages means `READY`.

## Analyst review and playback implementation boundary

The current pilot UI is plain HTML/CSS/JavaScript served by FastAPI because no browser application or build scaffold existed. It has accessible queue/detail navigation, final redacted transcript evidence, separate disposition/findings/review displays, reasoned ACCEPT/OVERRIDE actions and non-scoring TRIAGE for NEEDS_REVIEW audits. Review rows and playback-grant events are append-only; score writes require the current audit/transcript revision and expected review version. Calls with superseded audit evidence do not allow review actions. TRIAGE preserves a null score and NEEDS_REVIEW decision and leaves the unresolved call visible in the queue; the UI prevents duplicate triage actions. It does not resolve findings or claim quality completion.

Audio playback is available only to QA_ANALYST, COMPLIANCE_OFFICER and ADMIN identities in this slice. The explicit `POST /v1/calls/{id}/audio-access` grant records `AUDIO_ACCESS_GRANTED` before returning a 60-second HMAC capability bound to organisation, user, call and audio object. `GET /v1/calls/{id}/audio` still requires the authenticated identity, rechecks role, tenant access and tombstone state, and supports bounded single byte ranges for native seeking. Refresh access before the capability expires. The URL carries a short-lived capability in its query string; reverse-proxy and application access logs must redact that parameter. The API has no separate bulk-export endpoint, but authorized playback clients can retain the bytes they receive; production controls must address playback capture, TLS, key management, immutable off-database audit export and approved retention. Backend synthetic tests exercise role denial, role revocation, tenant and call binding, event insertion, range streaming and expiry; the browser smoke check exercises the grant/renew UI with mocked API fixtures. Browser UI implementation is vanilla JavaScript for this pilot; the planned React/TypeScript build remains a frontend replacement path, and there is no `npm` build or production browser qualification claim.

### Quality reports and findings export

`GET /v1/me/scores` exposes only the authenticated agent's current-transcript audits with a recorded ACCEPT/OVERRIDE review. It keeps machine and reviewed scores distinct and redacts checklist reasons, machine coaching text and the latest human review reason before returning them. Older review reasons and out-of-scope agents' notes are not returned. A redaction failure fails the report closed. `GET /v1/reports/team` derives teams from identity lookup, groups results by rubric and model runtime, and suppresses all cohort metrics when fewer than five distinct agents are represented. The 366-day report range is bounded; operators must not combine this aggregate with external data to re-identify individuals.

`GET /v1/exports/findings` is limited to COMPLIANCE_OFFICER, current transcript revisions, 92 days and 10,000 rows. The immutable `FINDINGS_EXPORTED` event is committed before response bytes are released. CSV contains call/team identifiers, rule/version metadata, finding state/evidence references and redacted remediation; it omits transcript text, utterance text, agent identifiers, audio and coaching notes. Textual labels/notes pass local redaction and spreadsheet formula neutralization. A redaction failure aborts the export. Store downloaded exports under the approved retention/access controls and avoid emailing them. A direct export is logged, but CSV content remains sensitive operational data. The quality page is static vanilla HTML/CSS/JavaScript; optional browser verification is `python -m pip install -e ".[browser-test]"`, `python -m playwright install chromium`, then `python -m unittest tests.test_browser_reports -v`. No npm build or production browser qualification is claimed.
