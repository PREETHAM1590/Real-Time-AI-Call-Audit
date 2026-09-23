# Real-Time AI Call Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an evidence-backed post-call QA and configurable disposition system, then extend it to live transcription, policy alerts and human review.

**Architecture:** Start with a modular Python backend, separate API/worker processes, self-hosted inference processes, PostgreSQL state/jobs/outbox, private audio storage and a React client. Reuse canonical transcript, disposition and audit contracts for uploaded recordings and live media. Disposition classification uses replaceable local typed-signal inference followed by deterministic rules in versioned JSON config; it is stored separately from the QA audit. Add large-scale infrastructure only after measured bottlenecks justify a separate subsystem plan.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic, psycopg, PostgreSQL, private S3-compatible storage, local faster-whisper/Whisper STT, local Qwen3-8B/vLLM audit candidate, Presidio, React/TypeScript, standard-library unittest, browser tests where UI behaviour needs them.

**Spec:** [Product and technical specification](../../system-specification.md). Read it together with [operations and evaluation](../../operations-and-evaluation.md), [self-hosted models](../../open-source-models.md) and the [user-provided disposition design package](../../Disposition_Platform_Design_Package/README.md).

## Global Constraints

- Use Python 3.12 or newer and TypeScript for application code.
- Store UTC timestamps and integer millisecond offsets from call start.
- Every persisted call-related record and query must carry an organisation scope.
- Treat interim transcripts and AI findings as provisional until their required evidence is final.
- Never send unredacted transcripts to the audit LLM, browser event feed, or ordinary application logs.
- Preserve machine audits and human reviews as separate immutable revisions.
- Do not assign PASS or FAIL when required evidence is missing or unreliable.
- Keep model artifact, inference runtime, prompt, rubric, ruleset and transcript revision identifiers with each audit.
- Self-host STT, diarization, sentiment, redaction, embeddings and audit inference; no hosted AI API fallback.
- Run disposition signal inference locally. TypeSafe System One/Jev is a typed-decision design reference, not a selected dependency. Do not assert its vendor-reported speed, cost, calibration or accuracy for this project.
- Keep disposition taxonomy/rules and its immutable result lineage separate from QA rubric scores/reviews. Never allow a disposition model to choose its own policy result without deterministic resolution.
- Pin model artifacts by revision and checksum, record licenses, and disable runtime downloads.
- Use synthetic data until real-data processing and retention policies are approved.

## Review Focus

1. Duplicate deliveries and worker crashes must produce one effective result per input revision — Task 2 integration check.
2. Unknown speakers, hold time and short calls must not produce unsupported disclosure decisions — Task 4 boundary check.
3. Prompt injection, invented citations and absent evidence must not become accepted scores — Task 5 validation check.
4. Cross-tenant IDs and simultaneous reviews must not expose data or overwrite analyst decisions — Tasks 1 and 6 checks.
5. Late finals, reconnects and dropped media must leave a visible incomplete/revised state — Tasks 7 and 8 replay checks.

---

## How to use this plan

This workspace contained no application, repository metadata or project instructions when inspected. All application paths below are **proposed new files**. This plan creates no cloud resources and does not authorise use of real recordings by itself.

Tasks 1–6 deliver a post-call application with a distinct disposition result; 7–8 deliver live monitoring; 9–10 complete the operational pilot. Execute Task 3A immediately after Task 3. Keep those boundaries independently demoable. Expansion features listed in the specification need separate plans; they are not hidden work inside Task 10.

Each numbered step is one reviewable action; larger implementation blocks are subdivided into focused changes. The code below pins important contracts and boundary behaviour; local model adapter wiring follows the selected runtime's current documented API and is validated by staging contract checks. Do not treat these excerpts as a complete application already written.

Run the named test before implementation and confirm it fails for the expected missing behaviour, then run it again after implementation. A broken environment is not a useful red test. Use standard-library `unittest` for backend checks; add only the browser test tooling needed for the actual UI. Commit after the task's checks pass, if a Git repository has been initialised. Do not create remote repositories or push as part of executing this plan.

## Proposed file structure

| Path | Responsibility | First task |
|---|---|---:|
| pyproject.toml, .env.example, .gitignore | Package, dev/test dependencies, configuration names, secret exclusions | 1 |
| app/__init__.py, app/config.py, app/contracts.py | Package, validated settings, canonical input/output models | 1 |
| app/auth.py, app/api.py | Identity/scope enforcement and HTTP endpoints | 1 |
| app/db.py, app/migrate.py, migrations/001_core.sql | Connection/transaction access, explicit schema migration command | 2 |
| app/storage.py, app/ingest.py, app/worker.py | Private audio, intake validation, durable stage execution | 2 |
| compose.yaml | Local PostgreSQL only | 2 |
| app/transcription.py, app/privacy.py | Local STT normalisation, role mapping and redaction | 3 |
| app/compliance.py, config/rules.v1.json | Pure call-scoped policy evaluation and approved versioned rules | 4 |
| app/audit.py, config/rubric.v1.json, prompts/audit.v1.txt | Local model request, evidence validation, deterministic scoring | 5 |
| app/disposition.py, app/disposition_config.py, config/disposition.v1.json | Typed signal contract, safe config validation, front gates, deterministic resolver and immutable trace | 3A |
| app/reviews.py | Immutable reviews and conflict control | 6 |
| web/package.json, web/package-lock.json, web/tsconfig.json, web/vite.config.ts, web/index.html | Browser build and test tooling | 6 |
| web/src/main.tsx, web/src/App.tsx, web/src/api.ts, web/src/styles.css | Accessible analyst application and typed API access | 6 |
| app/media.py | Telephony session authentication, frame ordering and local STT draining | 7 |
| config/models.lock.json | Exact local weight revisions/checksums/licenses and runtime versions | 3 |
| app/events.py, app/sentiment.py, web/src/LiveCalls.tsx | Resumable scoped feed, sentiment and supervisor UI | 8 |
| app/reports.py, web/quality.html, web/quality.js | Own-score/team views and safe exports | 9 |
| app/retention.py, app/metrics.py, app/evaluate.py | Purging, operational measurements and quality evaluation | 10 |
| Dockerfile, .github/workflows/ci.yml | Container packaging and CI if hosted on GitHub | 10 |
| tests/__init__.py, tests/test_*.py, tests/fixtures/ | Runnable contracts, integration cases and synthetic fixtures | 1–10, 3A |
| web/tests/qa.spec.ts, web/tests/live.spec.ts | Critical browser flows | 6, 8 |

No repository/helper already exists to reuse. Use Pydantic for validation already needed by FastAPI, PostgreSQL constraints for uniqueness, native audio controls for playback and plain SQL migrations. Keep one concrete local inference adapter per task and avoid a second queue service in the pilot.

## Task 1: Establish typed contracts and scoped access

**Files:** Create package/config/auth/API files from the structure table; `tests/__init__.py`, `tests/test_contracts.py`.

**Interfaces:**

- `Scope(organisation_id: str, user_id: str, role: str, team_ids: frozenset[str])` immutable dataclass.
- `can_access(scope: Scope, organisation_id: str, agent_id: str, team_id: str) -> bool`.
- `Utterance(id: str, role: str, start_ms: int, end_ms: int, text_redacted: str, is_final: bool = True)` Pydantic model. The persisted envelope adds organisation/call/revision/local-model fields from the spec.
- `create_app(settings: Settings) -> FastAPI`; shared scope dependency validates production OIDC/session identity. Test injection is confined to test construction.

- [x] **1. Write the contract check** in `tests/test_contracts.py`:

```python
import unittest
from pydantic import ValidationError
from app.auth import Scope, can_access
from app.contracts import Utterance

class ContractTests(unittest.TestCase):
    def test_scope_and_invalid_time(self):
        agent = Scope("org-a", "agent-a", "AGENT", frozenset())
        self.assertTrue(can_access(agent, "org-a", "agent-a", "team-a"))
        self.assertFalse(can_access(agent, "org-b", "agent-a", "team-a"))
        self.assertFalse(can_access(agent, "org-a", "agent-b", "team-a"))
        with self.assertRaises(ValidationError):
            Utterance(id="u1", role="AGENT", start_ms=20,
                      end_ms=10, text_redacted="Hello")
```

- [x] **2. Run:** `python -m unittest tests.test_contracts -v`; the initial test established the expected failure before implementation.
- [x] **3. Implement contracts** with `extra="forbid"`, bounded text, allowed roles `AGENT/CUSTOMER/UNKNOWN/IVR`, nonnegative offsets and `end_ms >= start_ms`.
- [x] **4. Implement scope enforcement** using this decision order:

```python
def can_access(scope, organisation_id, agent_id, team_id):
    if scope.organisation_id != organisation_id:
        return False
    if scope.role == "AGENT":
        return scope.user_id == agent_id
    if scope.role == "TEAM_LEADER":
        return team_id in scope.team_ids
    return scope.role in {"QA_ANALYST", "COMPLIANCE_OFFICER", "ADMIN"}
```

This grants record visibility only; audio/review/export permissions remain separate. Verify agent identity mapping against the identity store, not a submitted request body.
- [x] **5. Wire validated settings, identity and health routes.** OIDC signature/issuer/audience/expiry, PostgreSQL-backed active-subject memberships, cookie origin/CSRF and pre-multipart upload authorization are covered; ambiguous or unavailable identity resolution fails closed.
- [x] **6. Run the module again**, record pinned package versions, then commit `feat: establish contracts and scoped access` and the reviewed auth follow-up. Sol approval and PostgreSQL `_test` results are recorded in the implementation handoff.

## Task 2: Build durable recording intake and job recovery

**Files:** Create database/migration/storage/ingest/worker/compose files; modify `app/api.py`; create `tests/test_ingest.py` and `tests/fixtures/short.wav` using Python's `wave` module and synthetic PCM.

**Interfaces:**

- `accept_recording(scope: Scope, external_ref: str, audio: bytes, metadata: dict, idempotency_key: str) -> dict` returns `id` and `processing_state`.
- `claim_job(connection, worker_id: str, lease_seconds: int = 60) -> dict | None`.
- `finish_job(connection, job_id: str, lease_token: str, result: dict) -> bool` returns false for stale lease/tombstone.
- `python -m app.migrate` applies versioned SQL; `python -m app.worker` drains jobs.

- [x] **1. Write the database recovery check** against isolated PostgreSQL:

```python
# In tests/test_ingest.py, after migrations and inserting one synthetic job:
with connect() as connection_a, connect() as connection_b:
    first = claim_job(connection_a, "worker-a", lease_seconds=60)
    self.assertIsNotNone(first)
    self.assertIsNone(claim_job(connection_b, "worker-b", lease_seconds=60))
    connection_a.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (first["id"],))
    connection_a.commit()
    second = claim_job(connection_b, "worker-b", lease_seconds=60)
    self.assertEqual(first["id"], second["id"])
    self.assertFalse(finish_job(connection_a, first["id"], first["lease_token"], {}))
    self.assertTrue(finish_job(connection_b, second["id"], second["lease_token"], {}))
```

Use `unittest.TestCase.setUp` to apply migrations to a dedicated test database and insert the organisation/call/job with explicit UUIDs; `tearDown` removes only those test IDs. Refuse the integration suite unless the database name ends in `_test`.
- [x] **2. Run:** `python -m unittest tests.test_ingest -v`; explicitly enabling `RUN_POSTGRES_INTEGRATION=1` requires `DATABASE_URL` naming an isolated `_test` database and must fail if the variable or database is missing; never treat a skipped integration suite as passed verification.
- [x] **3. Create schema** for the tables and composite keys in spec §8. Claim due work in a short transaction:

```sql
WITH selected AS (
  SELECT id FROM jobs
  WHERE (state = 'QUEUED' AND available_at <= now())
     OR (state = 'RUNNING' AND lease_until < now())
  ORDER BY available_at, id
  FOR UPDATE SKIP LOCKED LIMIT 1
)
UPDATE jobs SET state = 'RUNNING', lease_token = %s,
  lease_until = now() + make_interval(secs => %s), attempts = attempts + 1
WHERE id IN (SELECT id FROM selected)
RETURNING *;
```

Update completion only when ID/token match, lease is unexpired, state is RUNNING and call is not tombstoned. Renew lease during long stages. Transactions that store stage output also enqueue the next stage. Keep migration versions immutable; follow-up schema changes use a new migration.
- [x] **4. Implement intake:** enforce request-body limits before multipart parsing and file-size limits while streaming; bound upload/decode concurrency. Inspect codec/duration and fully decode in a bounded worker, generate storage keys server-side, compute SHA-256, and commit call/job only after verified private upload. Derive agent/team from authenticated server identity/membership. Same organisation/idempotency key plus different payload returns 409. Expired staged objects are cleaned up. **Release gate remains:** OS-enforced decoder CPU/memory limits are not implemented; do not process real data or claim production readiness until the decoder is isolated with those limits.
- [x] **5. Add assertions:** duplicate/concurrent idempotency returns the same call; changed bytes return 409; spoofed/truncated media and over-limit bodies fail before unbounded spooling; CORS preflight permits the idempotency header; tombstoned jobs do not starve live jobs; unsupported stages park without consuming retry attempts; two independent workers cannot own the same lease; expired leases recover; persisted call detail is scoped. Storage failure leaves no runnable job.
- [x] **6. Run unit and full migration-upgrade checks**, simulate worker loss by expiring a lease, then reclaim the job from a second independent connection. Explicitly run PostgreSQL tests against an isolated `_test` database with no skips, then commit `feat: add durable recording intake`.

## Task 3: Transcribe and enforce the privacy boundary

**Files:** Create `app/transcription.py`, `app/privacy.py`, `tests/test_transcription.py`; modify worker/storage/contracts.

**Interfaces:**

- `normalise_segments(response: dict, channel_roles: dict[int, str]) -> list[dict]` normalises local-model timing and retains UNKNOWN roles.
- `redact_text(text: str) -> str` uses configured PII recognition and structured patterns.
- `prepare_utterances(segments: list[dict], redact: Callable[[str], str]) -> list[Utterance]` requires a redactor; there is no identity-redactor default.
- `transcribe_recording(private_key: str, language: str) -> list[dict]` is the pinned local faster-whisper adapter.

- [ ] **1. Add an executable privacy boundary check:**

```python
import unittest
from app.transcription import prepare_utterances

class PrivacyTests(unittest.TestCase):
    def test_unknown_role_and_redaction_failure(self):
        segments = [{"id": "u1", "role": "UNKNOWN", "start_ms": 0,
                     "end_ms": 1000, "text": "Email alice@example.test"}]
        rows = prepare_utterances(segments, lambda text: text.replace("alice@example.test", "[EMAIL]"))
        self.assertEqual(rows[0].role, "UNKNOWN")
        self.assertNotIn("alice@", rows[0].text_redacted)
        def failed_redaction(text):
            raise RuntimeError("redactor unavailable")
        with self.assertRaises(RuntimeError):
            prepare_utterances(segments, failed_redaction)
```

- [ ] **2. Run:** `python -m unittest tests.test_transcription -v`; expect missing adapter/preparation function.
- [ ] **3. Implement the pure boundary:**

```python
def prepare_utterances(segments, redact):
    return [Utterance(
        id=s["id"], role=s.get("role", "UNKNOWN"),
        start_ms=s["start_ms"], end_ms=s["end_ms"],
        text_redacted=redact(s["text"]), is_final=True,
    ) for s in segments]
```

Build the entire redacted batch before starting its persistence transaction. Do not log source segments or exception payloads.
- [x] **4. Implement local STT and production redaction adapters.** The pinned local faster-whisper runtime and fail-closed Presidio/structured redactor verify absolute artifact paths and SHA-256, with runtime downloads disabled. Actual model artifacts are not provisioned; compute sensitive-number signals before redaction and persist no raw word-level content. Mono/stereo roles remain UNKNOWN unless trusted channel mapping is supplied.
- [x] **5a. Add synthetic tests** for PII fixtures, mono/stereo unknown roles, channel separation, overlapping timings, and raw-secret/log absence.
- [ ] **5b. Run the pinned local model on approved samples** with outbound internet blocked; record actual WER, role/timestamp behaviour, runtime and GPU use. **Pending release gate:** no approved samples, pinned model artifacts, or target hardware are available in this workspace.
- [x] **6. Run module and migration-upgrade tests** against an isolated `_test` database; verify raw text has no database column or log sink; then commit `feat: add private local transcription pipeline` and `fix: transcribe stereo channels independently`.

## Task 3A: Identify configurable dispositions from final redacted evidence

**Files:** Create `app/disposition.py`, `app/disposition_config.py`, `config/disposition.v1.json`, `tests/test_disposition.py`; add tenant-scoped immutable `disposition_configs` and `dispositions` tables to the next SQL migration; extend the post-call worker/API to persist and return disposition separately from the audit.

**References:** Follow the user-provided [PRD](../../Disposition_Platform_Design_Package/PRD_Configurable_Disposition_Platform.docx), [TRD](../../Disposition_Platform_Design_Package/TRD_Configurable_Disposition_Platform.docx), [system design](../../Disposition_Platform_Design_Package/System_Design_Configurable_Disposition_Platform.docx), [JSON guide](../../Disposition_Platform_Design_Package/JSON_Configuration_Guide.docx), and sample schema/config. Treat the documents as proposed product requirements; follow this repository's self-hosted model and privacy constraints where they conflict with the hosted Jev sample.

**Interfaces:** `compile_disposition_config(raw: dict) -> CompiledDispositionConfig`; `resolve_disposition(config, signals, authoritative_facts) -> DispositionDecision`; `classify_disposition(transcript: list[Utterance], facts: dict, adapter: LocalTypedDecisionAdapter) -> DispositionResult`. Canonical signals use typed `noul`, `choice` or `score` values with finite probabilities; the model adapter is replaceable and receives only final redacted utterances. The model proposes signals; deterministic front gates and priority-sorted declarative rules choose a configured taxonomy code.

- [x] **1. Add a failing deterministic boundary check** for authoritative front-gate precedence, priority order, missing/invalid signal → REVIEW, and reproducibility from identical config/signals. Include a fake local adapter contract and assert the fake receives only final redacted text.
- [x] **2. Run:** `python -m unittest tests.test_disposition -v`; confirm the new test fails because the module is absent.
- [x] **3. Compile configuration before use:** validate schema and cross-references (unique codes/question IDs/rule IDs/priorities, acyclic parent links, valid emits, compatible operators/types, bounded thresholds); reject unknown keys and all executable expressions/URLs/credentials. Store immutable tenant-scoped version + content hash; only an approved version is active.
- [x] **4. Implement normalized typed-signal validation and a single local inference adapter.** Keep provider/model selection outside business JSON and in deployment config. Do not claim model-calibrated confidence until the selected pinned open model passes calibration evaluation; invalid, missing or nonfinite outputs become review. Use deterministic fakes in CI; no hosted fallback or runtime downloads.
- [x] **5. Implement deterministic resolution:** authoritative system facts may satisfy validated front gates; otherwise evaluate rules by explicit numeric priority, then confidence policy. Missing evidence, unknown role, incomplete call, conflicting signals or no qualifying rule resolves to configured review/unknown behavior. Add no generic expression language or second queue/database.
- [x] **6. Persist immutable revision/provenance:** organisation/call/transcript revision, config ID/version/hash/schema, local model artifact + adapter version, canonical signals, processing path, matched rule, confidence/review status and usage. A reprocessing revision creates a new disposition record; it never overwrites a prior result. Enforce organisation scope with composite keys and idempotent stage completion.
- [x] **7. Add scoped APIs** per system-specification §9: get result/config versions, validate without storage, stage immutable versions, record a reasoned immutable approval from an ADMIN other than the creator, activate only approved versions by compare-and-swap, replay synthetic/approved redacted fixtures, and rollback only to previously active approved history with a required reason. Return field errors (422), auth errors (401/403), stale-pointer conflicts (409); never edit version rows/events. The pilot's approval action is this explicit second-ADMIN event; status/approver fields are derived from append-only events. Every administrative mutation is access-logged. Provide fixture replay metrics, but do not allow per-config deployment flags to bypass platform privacy/model policy.
- [x] **8. Run unit, migration and worker integration checks** for tenant isolation, duplicate execution, partial transcript exclusion, invalid model output, front-gate no-inference, immutable revisions and config rollback. Test only on an isolated database whose name ends `_test`; run backend suite and fix regressions.
- [x] **9. Run synthetic end-to-end classification through the post-call worker and scoped call detail API.** Report dispositions as a distinct result from seven-dimension audit/review; then commit `feat: add configurable disposition identification`.

**Initial implementation boundary:** keep one active config per organisation until the platform has an authoritative call-to-use-case routing key and use-case membership policy. `config_id` and `use_case_id` are business identifiers only; organisation identity remains server-derived. Group adjacent canonical utterances by known speaker ID and role before bounded window packing; never split a turn, count overlap against the per-request character ceiling, and retain member utterance IDs for evidence. Bound config JSON bytes, depth, container sizes and persisted integer ranges. Use bounded character counts for per-request windows and full-call limits, cap chunks at 128, and document that these are not tokenizer-measured budgets. The local vLLM model manifest must report both the configured model ID and the exact verified artifact root path; mismatches fail closed. In this slice `ANALYSE` means disposition only and returns `NEEDS_REVIEW`; the later policy and QA stages must be implemented before any call can become `READY`.

## Task 4: Implement versioned policy evaluation

**Files:** Create compliance module/config and `tests/test_compliance.py`; modify worker.

**Interfaces:** `disclosure_state(utterances: list[Utterance], opportunity_ms: int, ended: bool, reliable: bool) -> str`; `evaluate_rules(utterances, context: dict, ruleset: dict) -> list[dict]` returns versioned findings. Context includes organisation/call/revision, agent connection, tagged holds, completeness and call type.

- [x] **1. Write timing/role checks:**

```python
import unittest
from app.compliance import disclosure_state
from app.contracts import Utterance

class DisclosureTests(unittest.TestCase):
    def test_window_and_unknown_evidence(self):
        self.assertEqual(disclosure_state([], 29000, False, True), "PENDING")
        self.assertEqual(disclosure_state([], 30000, False, True), "POTENTIAL_VIOLATION")
        self.assertEqual(disclosure_state([], 10000, True, True), "UNKNOWN")
        self.assertEqual(disclosure_state([], 30000, True, False), "UNKNOWN")
        customer = Utterance(id="u1", role="CUSTOMER", start_ms=0,
            end_ms=2000, text_redacted="This call may be recorded")
        self.assertEqual(disclosure_state([customer], 30000, True, True), "POTENTIAL_VIOLATION")
```

- [x] **2. Run:** `python -m unittest tests.test_compliance -v`; expect missing rule evaluator.
- [x] **3. Implement temporal state** after filtering final agent text to the opportunity window and joining adjacent eligible segments:

```python
def disclosure_state(utterances, opportunity_ms, ended, reliable):
    if not reliable:
        return "UNKNOWN"
    text = " ".join(u.text_redacted for u in utterances
                    if u.is_final and u.role == "AGENT")
    if "may be recorded" in text.casefold():
        return "SATISFIED"
    if opportunity_ms >= 30000:
        return "POTENTIAL_VIOLATION"
    return "UNKNOWN" if ended else "PENDING"
```

This helper's input must already be clipped to the eligible window. `evaluate_rules` computes the window from agent connection/hold intervals and passes only qualifying utterances; test this caller separately so a late disclosure cannot satisfy an opening obligation. Configure approved phrase variants in the ruleset rather than expanding this illustrative phrase silently.
- [x] **4. Implement call-scoped finding upsert** keyed by call/transcript/ruleset/rule/evidence. Add closing checks at drain completion, advisory sensitive-number flags and safe remediation text. Do not equate a regex hit with a confirmed regulatory violation.
- [x] **5. Add boundary checks:** phrase split across two final agent segments satisfies; partial phrase does not; late phrase does not; hold exclusion uses tagged intervals; two calls cannot share triggered state; incomplete audio returns UNKNOWN.
- [x] **6. Run module**, record example policy as synthetic-only until approved, then commit `feat: add timed policy evaluation`.


**Implemented limitation/release gate:** upload intake has no trusted source for agent connection, hold intervals, call completeness or call type. These columns default to unreliable; timed disclosure/closing findings stay `UNKNOWN` until a trusted telephony context integration and its verification are implemented. Caller-supplied audio metadata does not establish timing reliability. Policy rulesets are synthetic examples until approved. No call becomes `READY` from policy findings.

## Task 5: Generate and validate evidence-backed audits

**Files:** Create audit module/prompt/rubric and `tests/test_audit.py`; modify contracts/worker.

**Interfaces:** `validate_evidence(evidence: list[dict], utterances: list[Utterance]) -> None`; `weighted_score(scores: dict[str, int | None], weights: dict[str, int]) -> float | None`; `audit_call(call: dict, utterances: list[Utterance], findings: list[dict]) -> dict` returns a validated immutable audit or review status.

- [x] **1. Write citation checks:**

```python
import unittest
from app.audit import validate_evidence
from app.contracts import Utterance

class AuditTests(unittest.TestCase):
    def test_invented_evidence_rejected(self):
        rows = [Utterance(id="u1", role="AGENT", start_ms=0,
                         end_ms=1000, text_redacted="I can help.")]
        validate_evidence([{"utterance_id": "u1", "quote": "can help"}], rows)
        with self.assertRaises(ValueError):
            validate_evidence([{"utterance_id": "u1", "quote": "refund approved"}], rows)
        with self.assertRaises(ValueError):
            validate_evidence([{"utterance_id": "other-call", "quote": "help"}], rows)
```

- [x] **2. Run the new checks first** and confirm the initial missing implementation fails.
- [x] **3. Implement strict evidence validation:**

```python
def validate_evidence(evidence, utterances):
    final = {u.id: u for u in utterances if u.is_final}
    for item in evidence:
        source = final.get(item["utterance_id"])
        quote = item["quote"]
        if source is None or not quote.strip() or quote not in source.text_redacted:
            raise ValueError("INVALID_EVIDENCE")
```

The caller fetches only the authorised call's pinned transcript revision. Validate score/evidence relationship and speaker role after substring validation; matching words alone do not establish the judgment's truth.
- [x] **4. Implement audit schema/prompt** using the seven exact IDs/weights from spec §10, strict enum/range/unique-dimension validation, explicit refusal/incomplete handling and server-derived identity/timestamps/decision. Character ceilings fail closed without truncation; they are not tokenizer-measured budgets.
- [x] **5. Implement bounded local vLLM calls**, total three attempts, one validation repair within that budget, persisted attempt/latency data and review on exhaustion. Verify a pinned local artifact and the serving manifest's model ID and artifact root. No runtime download or hosted fallback; never log raw request/response bodies.
- [x] **6. Add score checks:** all scores 3 gives 3.00; objection N/A requires a server fact; missing required score yields null/NEEDS_REVIEW; duplicate dimensions, nonfinite scores, out-of-range values and unsupported citations are rejected. Transcript instructions remain untrusted data.
- [x] **7. Run the full suite with PostgreSQL integrations on an isolated `_test` database**, then commit `feat: add validated call audits`.

**Implemented limitation/release gate:** audit code, persistence and worker integration use deterministic fakes in tests; database triggers reject audit updates/deletes, and immutable identity includes inference runtime and prompt version as well as their hashes. Malformed model identifiers fail closed, and no-agent transcripts abstain without a coaching claim. No approved production model artifact, hardware, quality dataset, token-budget calibration or load/latency measurement exists. Calls remain `NEEDS_REVIEW` because the analyst review stage is Task 6 and trusted timed-policy inputs are not integrated. This slice does not establish production readiness.

## Task 6: Deliver analyst review and evidence navigation

**Files:** Create `app/reviews.py`, `app/audio_access.py`, migration 010–011, `tests/test_reviews.py`, `tests/test_audio_access.py`, `tests/test_browser_review.py` and the `web/` analyst page; modify audit/API and project test dependencies.

**Approved implementation adjustment:** The repository has no existing browser application or npm build scaffold. For the pilot review surface, use native HTML/CSS/JavaScript served by FastAPI and a Python Playwright browser smoke test (`.[browser-test]` plus Chromium). This avoids introducing a speculative frontend dependency tree; a React/TypeScript client remains a later replacement path and is not claimed as delivered.

**Interfaces:** `append_review(connection, scope: Scope, audit_id: str, base_review_version: int, action: str, scores: dict, reason: str) -> dict`; `ReviewConflict` maps to HTTP 409. The delivered vanilla JavaScript client uses `request()` for the authenticated API and `loadCall()` for call detail; there is no TypeScript interface or npm build in this pilot.

- [x] **1. Write concurrent-review integration check:**

```python
first = append_review(connection, qa_scope, audit_id, 0, "ACCEPT", {}, "Verified evidence")
self.assertEqual(first["version"], 1)
with self.assertRaises(ReviewConflict):
    append_review(connection, qa_scope, audit_id, 0, "OVERRIDE", {"clarity": 2}, "Not clear")
stored = connection.execute("SELECT overall_score FROM audits WHERE id = %s", (audit_id,)).fetchone()
self.assertEqual(stored[0], original_machine_score)
```

Create `qa_scope` with Task 1's `Scope`, insert a completed synthetic call/audit in setUp and use Task 2's isolated database restriction.
- [x] **2. Run:** `python -m unittest tests.test_reviews -v`; the expected missing implementation failure was recorded before implementation.
- [x] **3. Implement atomic review append:** lock audit and call rows, verify organisation/permission/current transcript and expected review version, validate changed scores and nonempty reason, insert immutable review and access event in one transaction. Recompute effective score using Task 5's scoring function and persisted rubric threshold. ACCEPT confirms the current vector; OVERRIDE merges changes over the prior effective vector. A reasoned TRIAGE action preserves a null score and NEEDS_REVIEW for unscoreable audits and keeps the call visible in the queue without offering duplicate triage actions.
- [x] **4. Build queue and detail screen:** display processing state, agent/date, final redacted transcript, findings, current disposition, and machine/review score separately. Evidence buttons focus the canonical transcript utterance; after explicit audio authorization they also seek the native `<audio controls>` player. Transcript content is rendered as text, not HTML. The queue panel also provides manual WAV/MP3 upload for an authenticated AGENT with exactly one server-resolved team; it uses the existing multipart endpoint, CSRF token and idempotency header with a 250 MiB client-side limit. It validates `.wav`/`.mp3` extensions, disables the full form while submitting, and reports 409 external-reference conflicts from atomic database insertion.
- [x] **5. Add review form** with required reason, saving/error state, conflict reload, keyboard focus and visible labels. Disable actions for superseded audit evidence. `POST /v1/calls/{id}/audio-access` records an immutable grant before a 60-second identity-bound URL is returned; range requests recheck current role, scope and tombstone. A renewal control is visible. AUDIO_ACCESS_GRANTED query capabilities must be redacted from proxy logs.
- [x] **6. Create browser check** against deterministic API fixtures using Python Playwright:

```powershell
python -m pip install -e ".[test,browser-test]"
python -m playwright install chromium
python -m unittest tests.test_browser_review -v
```

The browser test supplies deterministic synthetic fixtures and does not disable authentication in the application. Backend API tests cover agent denial, expired/wrong identity capabilities, permission revocation, logged grant and range streaming; PostgreSQL tests cover immutable review persistence and optimistic conflicts. The vanilla UI has no npm build. The Playwright smoke test is not a production-browser/accessibility qualification.
- [x] **7. Run:** full backend suite and browser smoke with all PostgreSQL integrations against a dedicated `_test` database, compileall and diff checks; no production-readiness claim. Commit the analyst review workflow.

## Task 7: Add one live media adapter and finalisation barrier

**Files:** Create `app/media.py`, `tests/test_media.py`; modify transcription/worker/API.

**Interfaces:** `FinalBuffer.apply(segment_id: str, text: str, final: bool) -> None`, `FinalBuffer.final_text() -> str`; `MediaSession.accept(frame: dict) -> list[bytes]` emits ordered validated audio; session identity includes a generation token so old reconnects cannot write to the active call.

- [x] **1. Write partial/final/deduplication check:** `tests/test_media.py` covers partial replacement, empty final output before finalization, duplicate finals, immunity to late partials and caller-supplied final ordering.

```python
import unittest
from app.media import FinalBuffer

class MediaTests(unittest.TestCase):
    def test_partial_replacement_and_final_deduplication(self):
        buffer = FinalBuffer()
        buffer.apply("s1", "may", False)
        self.assertEqual(buffer.final_text(), "")
        buffer.apply("s1", "may be recorded", True)
        buffer.apply("s1", "may be recorded", True)
        buffer.apply("s1", "late provisional text", False)
        self.assertEqual(buffer.final_text(), "may be recorded")
```

- [x] **2. Run:** `python -m unittest tests.test_media -v`; the test-first run failed because `app.media` did not exist, then passed after implementation.
- [x] **3. Implement segment finality:** `app.media.FinalBuffer` replaces partials, promotes a segment once, ignores later partials and duplicate finals, and joins finals in first-finalized order. The helper has no timestamps, so callers must supply canonical order; timestamp sorting remains at the persistence boundary.

```python
class FinalBuffer:
    def __init__(self):
        self.partials = {}
        self.finals = {}

    def apply(self, segment_id, text, final):
        if segment_id in self.finals:
            return
        if final:
            self.finals[segment_id] = text
            self.partials.pop(segment_id, None)
        else:
            self.partials[segment_id] = text

    def final_text(self):
        return " ".join(self.finals.values())
```

Use canonical timestamp sorting when persisting utterances; the helper receives ordered segments. Corrections to already-final local segments are explicit transcript revisions, not silent replacement.

**Task 7 status:** provider-independent `FinalBuffer` steps 1–3 are implemented and checked. No telephony adapter, worker/API integration, or DRAINING workflow is implemented by this slice.
- [ ] **4. Implement one telephony media adapter** after inspecting that platform's current authentication and media format. Validate signature/session scope, track IDs, codec, sequence, timestamps, payload length and base64. Bound input queue by bytes and time; 200-ms reorder buffer initially. Reject replayed sessions and mark gaps instead of filling fabricated audio. Send bounded overlapping speech windows from local Silero VAD to local faster-whisper; record window-to-stable-final latency and correction rate.
- [ ] **5. Implement DRAINING:** run a final local decode/reconciliation at call end; wait for a benchmarked bounded interval, then audit or mark incomplete on timeout. Flush stable redacted utterances transactionally before enqueuing audit. A late correction creates a new revision; stale generation frames are rejected. Never treat each window's raw Whisper text as final solely because inference returned.
- [ ] **6. Extend checks:** out-of-order and duplicate frames, missing frame, invalid signature, queue overflow, disconnect during drain and late final after timeout. Confirm provisional policy alerts are retractable and final findings are call-scoped.
- [ ] **7. Run module plus rules/audit regression**, replay a synthetic five-minute recording at real speed on target local hardware with internet blocked, report audio-to-stable-final latency and GPU use, then commit `feat: add live media ingestion`.

## Task 8: Add sentiment and a resumable supervisor view

**Files:** Create events/sentiment modules, LiveCalls component, `tests/test_live.py`, `web/tests/live.spec.ts`; modify API/App and the post-call disposition stage.

**Interfaces:** `sentiment_drop(previous: list[float], current: list[float]) -> bool` operates on already eligible final customer scores; `read_events(connection, scope: Scope, after_sequence: int, limit: int = 100) -> list[dict]`; `GET /v1/events` uses the spec envelope.

- [x] **1. Write sentiment boundary check:** `tests/test_live.py` covers sample minimum, the 0.4 threshold, below-threshold behavior and invalid values.

```python
import unittest
from app.sentiment import sentiment_drop

class LiveTests(unittest.TestCase):
    def test_insufficient_samples_and_drop(self):
        self.assertFalse(sentiment_drop([0.5], [-0.5]))
        self.assertTrue(sentiment_drop([0.5, 0.5, 0.5], [-0.1, -0.1, -0.1]))
        self.assertFalse(sentiment_drop([0.2, 0.2, 0.2], [0.1, 0.1, 0.1]))
```

- [x] **2. Run:** `python -m unittest tests.test_live -v`; the test-first run failed because `app.sentiment` did not exist, then passed after implementation.
- [x] **3. Implement sentiment eligibility and window logic:** `app.sentiment.sentiment_drop` validates signed finite scores in [-1, 1], requires at least three values on each side and returns true for an average drop of at least 0.4. `sentiment_alert_times` accepts already-inferred signals with a stable utterance `id`, `signed_score = p_positive - p_negative` and `top_class_probability`; it validates mappings, rejects missing/invalid or duplicate IDs, and validates eligible final CUSTOMER offsets and values. It groups by `start_ms` into half-open fixed 30-second windows, requires three unique utterances per window, a mean drop of at least 0.4 and an average top-class probability of at least 0.7, then applies a 90-second cooldown between alert-window end offsets. It returns the later window's end offset. This is pure decision logic; model inference, sentiment aggregation and event delivery remain pending.

```python
from statistics import mean

def sentiment_drop(previous, current):
    return (len(previous) >= 3 and len(current) >= 3
            and mean(previous) - mean(current) >= 0.4)
```

The pure helper applies these eligibility rules to already-inferred synthetic signals. Model selection/inference, probability generation, model-version provenance and live event delivery remain pending.
- [ ] **4. Integrate one evaluated sentiment model** for utterance probabilities; compute turn and call aggregates as specified. Warm it before readiness. An unavailable sentiment model does not block deterministic policy checks; show unknown sentiment.
- [x] **5. Implement scoped post-call SSE state refresh:** migration 015 adds a tenant-local transactional sequence and a `call.updated` outbox containing only processing state and transcript revision. Intake, committed worker stages, retries, exhausted-job failures, and ACCEPT/OVERRIDE/TRIAGE review commits append in the same transaction. SSE revalidates the signed token and current server-owned membership/team scope before each event; the outbox retains at most 10,000 events per tenant, and call purge removes its events and advances the cursor floor so older clients receive `reset_required`. `GET /v1/events` resumes by `Last-Event-ID`, rejects future/invalid cursors, reads at most one row per authorized pull, and stops on disconnect. This is a post-call refresh feed only: live media, sentiment events, supervisor cards, and browser reconnect behavior are not implemented.
- [ ] **6. Build active-call cards** with textual policy/sentiment state, evidence, acknowledgement and stale indicator. Browser test disconnects feed, expects “Reconnecting”, reconnects with previous cursor and asserts one finding after a replayed event. Backend test attempts another organisation's cursor/call and receives no data.
- [ ] **7. Run backend checks and browser live check**, measure final-arrival-to-render latency, then commit `feat: add live supervisor monitoring`.
- [ ] Re-run disposition only after the live finalisation barrier on the final transcript revision; late corrections create a new immutable disposition revision and emit `disposition.ready`. Partial transcripts never create durable dispositions. Test normal completion, timeout/NEEDS_REVIEW, late correction and reconnect.

**Task 8 status:** provider-independent Steps 1–3 and Step 5's post-call state feed are implemented. No sentiment model integration, live media/event workflow, supervisor cards or browser reconnect behavior is implemented by these slices.

## Task 9: Deliver agent scores, trends and safe exports

**Files:** Create `app/reports.py`, `migrations/012_findings_export_events.sql`, `web/quality.html`, `web/quality.js`, `tests/test_reports.py`, and `tests/test_browser_reports.py`; modify `app/api.py`, `tests/test_contracts.py`, and report styles.

**Interfaces:** `csv_cell(value: str) -> str`; `own_scores(connection, scope: Scope) -> list[dict]`; `team_report(connection, scope: Scope, start: datetime, end: datetime) -> dict`. Aggregates expose sample counts and group/filter by rubric/model versions.

- [x] **1. Write export injection check:**

```python
import unittest
from app.reports import csv_cell

class ReportTests(unittest.TestCase):
    def test_formula_prefixes(self):
        for value in ["=1+1", "+cmd", "-1", "@SUM(A1)", " \t=1+1"]:
            self.assertTrue(csv_cell(value).startswith("'"))
        self.assertEqual(csv_cell("Coaching note"), "Coaching note")
```

- [x] **2. Run:** `python -m unittest tests.test_reports -v`; the first run failed because `app.reports` was not implemented yet.
- [x] **3. Implement safe text cell conversion**, then let Python's `csv` library escape commas/quotes:

```python
def csv_cell(value):
    stripped = value.lstrip()
    return "'" + value if stripped.startswith(("=", "+", "-", "@")) else value
```

- [x] **4. Implement scoped reports:** own-score endpoint derives agent identity; team query uses the server-authorized team set. Report reviewed vs machine scores separately, count and group by rubric/model versions; suppress cohort metrics below five distinct agents. Include checklist, redacted machine coaching text and only the latest redacted human review reason in the own view.
- [x] **5. Add bounded redacted CSV export** restricted to the compliance role, with immutable access-event logging, safe filenames, a 92-day window and a 10,000-row cap. Integration assertions cover own-only data, team scope, empty periods, version separation, export bounds, redaction and formula-safe cells.
- [x] **6. Run module and browser checks** with synthetic agent/team views. The pilot uses the existing dependency-free vanilla HTML/CSS/JS approach instead of the originally proposed React/TypeScript build because no frontend build scaffold exists; replace it when a product frontend is selected. `tests.test_browser_reports` uses the optional pinned Playwright extra and Chromium, so there is no npm build command for this slice. Commit `feat: add scoped quality reporting`.

## Task 10: Qualify retention, recovery, quality and release

**Files:** Create retention/operations/evaluation modules, `tests/test_operations.py`, `tests/test_observability.py`, `Dockerfile`, `uv.lock`, runtime entrypoints, container/CI files and `tests/fixtures/golden.jsonl`; update operations guide with actual commands and measured results. Golden data committed to Git is synthetic/redacted, not customer recordings.

**Interfaces:** `due_for_deletion(expires_at: datetime, now: datetime, legal_hold: bool) -> bool`; `request_call_deletion(...)` tombstones an ADMIN-requested deletion; `tombstone_expired_calls(..., limit<=100)` applies bounded expiry tombstones; `purge_call(connection, organisation_id: str, call_id: str, storage) -> dict` removes mutable content after a tombstone and reports retained immutable-history counts; `python -m app.evaluate --dataset PATH --output PATH` writes sample counts, coverage, per-rule precision/recall and score agreement. Evaluation reports are artifacts rather than modifying expected labels.

**Implemented pilot slice (2026-09-23):** migrations 013–014 add retention/hold/tombstone lifecycle fields and structured immutable access-event details. The ADMIN DELETE API only requests deletion and tombstones immediately; bounded expiry and purge operations are library functions, not a scheduled worker or an operator purge endpoint. Purge removes audio objects, transcript utterances, findings and jobs, and scrubs mutable call identity fields. It deliberately preserves immutable audits, dispositions, reviews and call-related access events; `access_events` counts direct CALL records, review records linked to this call's audit IDs, audio grants linked to its audio-object IDs, and the purge record. Even an unaudited call returns `PARTIAL_IMMUTABLE_HISTORY`. Full erasure remains gated on an approved, verifiable immutable-history retention or crypto-erasure policy. No expiry policy is assigned automatically. Observability now includes safe worker stage-outcome logs, DB-only `/ready`, and an ADMIN-only tenant aggregate; it does not provide time-series metrics, alerts, spend data or model readiness. Runtime packaging now includes a locked Python dependency graph, `app.main:app`, a continuously polling bounded-idle worker, and one locally built non-root backend image. Compose remains PostgreSQL-only; CI is not configured. The image includes runtime libraries and ffmpeg but no model weights or secrets. Task 10 does not qualify production retention, recovery, hardware, model quality, capacity or deployment.

**Verification evidence:** on 2026-09-23, `python -m compileall -q app tests`, `python -m app.evaluate --dataset tests/fixtures/golden.jsonl --output <temporary report>`, `git diff --check`, `uv lock --check`, focused runtime entrypoint tests, and `python -m unittest discover -s tests -v` passed. The latest full run was 137/137 with integrations enabled against a newly created `call_audit_runtimepkg_20260923_test` PostgreSQL database and `RUN_POSTGRES_INTEGRATION=1`. A local `docker build -t call-audit:local .` succeeded; `docker run --rm --entrypoint id call-audit:local` reported UID/GID 10001, and the container imported `app.main` using synthetic settings. The retention regression includes review and audio-grant event lineage. The report is a synthetic fixture smoke test only; its counts are not model-quality measurements. No restore, load, pinned-model quality, CI workflow, deployment or production-deletion qualification ran.

- [x] **1. Write retention boundary check:** exact-expiry, hold, non-due and timezone-awareness checks in `tests/test_operations.py`.

```python
import unittest
from datetime import datetime, timedelta, timezone
from app.retention import due_for_deletion

class RetentionTests(unittest.TestCase):
    def test_expiry_and_hold(self):
        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        self.assertTrue(due_for_deletion(now, now, False))
        self.assertFalse(due_for_deletion(now, now, True))
        self.assertFalse(due_for_deletion(now + timedelta(seconds=1), now, False))
```

- [x] **2. Run:** `python -m unittest tests.test_operations -v`; the initial test-first run failed before the modules existed; focused reruns now pass. PostgreSQL lifecycle checks require `RUN_POSTGRES_INTEGRATION=1` and a database ending `_test`.
- [x] **3. Implement lifecycle predicate and bounded tombstone-first partial purge:**

```python
def due_for_deletion(expires_at, now, legal_hold):
    if expires_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("UTC-aware timestamps required")
    return not legal_hold and expires_at <= now
```

Purge follows tombstoning and is idempotent. The current implementation preserves immutable audit/disposition/review/access rows, so it is partial content purge and does not meet a full-erasure policy. Existing worker tombstone checks are covered by a race regression. The lifecycle functions are not scheduled or exposed as a purge endpoint.
- [x] **4a. Add safe stage-outcome logging and limited operational visibility:** worker logs contain stage, bounded attempt, outcome and duration only; `/ready` reports database availability with model readiness unknown; ADMIN-only `/v1/operations/summary` returns tenant-scoped pending-job count, bounded oldest age and non-READY call count. Tests verify no IDs/error content in logs or responses, tenant scoping and role protection.
- [ ] **4b. Complete observability:** a metrics exporter/time-series store, alerting, per-stage dashboards and spend/capacity telemetry are still required. No model-readiness or GPU/compute readiness claim is implemented.
- [x] **5. Implement deterministic evaluation report** with explicit denominator rules:

```python
def precision_recall(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {"precision": precision, "recall": recall}
```

Undefined ratios remain null, never perfect scores. Score agreement uses only adjudicated applicable dimensions; report abstentions and excluded counts alongside it. The committed JSONL fixture is synthetic and contains a zero-positive case, a missed critical finding, an abstention and an excluded case. The strict input rejects transcript/raw-text fields. The report is deterministic and includes a dataset hash.
- [x] **6a. Package the runtime:** pin `uvicorn`, generate `uv.lock`, provide `app.main:app` and a continuous bounded-idle worker entrypoint, and build one non-root Docker image. Compose remains PostgreSQL-only because native API/worker commands are sufficient for local development. Runtime smoke tests and a local image build verified UID/GID 10001. No models or secrets are copied into the image.
- [ ] **6b. Add repository CI:** no remote/native CI is configured, so workflow setup, isolated PostgreSQL service checks, browser checks and secret scanning remain pending.
- [ ] **7. Run full operational verification:** backend suite and migration checks are runnable; browser/report suite and synthetic evaluator are available. Pinned-model contract/quality, 100-call/25-viewer load, selected-hardware percentiles, memory use and error-rate measurements remain unrun release gates.
- [ ] **8. Perform restore/deletion drills** in staging, approve immutable-history disposition and retention, confirm role controls/redaction with QA/privacy owner, record exact licenses/artifact checksums, offline inference proof and compute cost projection, then follow staged rollout. These are not evidenced by local synthetic tests.
- [ ] **9. Commit** `feat: qualify call audit operations` only after recording failures and resolved gates. A failing operational gate keeps production release pending; synthetic demonstration can still be complete.

## Requirement and source coverage review

| Source topic | Disposition |
|---|---|
| Business problem, QA workflow and explainability | Spec §§1–4; Tasks 5–6 |
| Upload/live ingestion, STT and speaker attribution | Tasks 2–3, 7 |
| Sentiment, policy and LLM scoring | Tasks 4–5, 8 |
| Configurable disposition taxonomy, typed signals, deterministic precedence, config lifecycle and model replacement | Spec §§6, 8; Task 3A; user-provided disposition design package |
| Analyst/supervisor/agent dashboards and trends | Tasks 6, 8–9 |
| Privacy, security, data model and override history | Tasks 1–3, 6, 10 |
| Testing, latency, reliability, cost, CI and observability | Task 10 and operations guide |
| SIPREC, additional vendors, secondary diarization/model | Explicit expansion subsystem plans |
| Local Whisper/Silero/pyannote/Presidio/Transformers/Qwen/vLLM | Candidate stack and measured selection in Tasks 3, 5, 7–8 and model guide |
| Kafka/warehouse/ClickHouse/Kubernetes/Temporal | Expansion after pilot bottleneck measurements |
| Fine-tuning, multilingual, predictive/whisper coaching and biometrics | Future research, not pilot release obligations |
| Interview advice, author biography and platform chrome | Reference-only; no application feature |

Self-review: requirement IDs R01–R15 map to tasks in the specification; core type/function names are consistent across tasks; high-risk review conditions have named checks; local model quality and hardware capacity are evaluation gates. The plan contains proposed tests, not claims that those tests have run.

## Handoff

Execution has been authorised. Use the user-selected subagent-driven workflow: GPT-6 Luna implements one scoped task at a time, then GPT-6 Sol reviews it before the next task starts. Preserve this plan's order and dependency boundaries; stop to fix blocking review findings rather than building dependent stages on top of them. The documented thresholds remain proposals until measured and approved.
