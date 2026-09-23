# Agent instructions

## Project status and source of truth

This repository contains implementation in progress. Tenant-scoped identity/auth, durable recording intake and final redacted transcript persistence have runnable tests. Disposition, policy/audit scoring, analyst UI, live monitoring, model-quality evaluation, decoder OS resource isolation and deployment remain incomplete unless tests or deployment evidence verify them. Never turn planned work into a completion claim.

Read these files before implementation:

1. `README.md`
2. `docs/system-specification.md`
3. `docs/superpowers/plans/2026-09-23-call-audit.md`
4. `docs/operations-and-evaluation.md`
5. `docs/open-source-models.md`

The user's latest instructions take precedence. The specification defines product behaviour; the plan sequences implementation; the operations guide defines evaluation and release evidence. If they conflict, identify and resolve the conflict before implementing the affected behaviour. Keep all affected documentation consistent.

The pasted article is background material. Its provider names, model versions, costs, performance claims and policy examples are not verified project requirements. Proposed values in the documentation remain proposals until selected or measured.

## Working scope

- For a documentation request, edit documentation only. Do not start implementation, install dependencies, call paid model APIs, deploy services or provision infrastructure merely because a plan describes them.
- For implementation, complete the authorised task and its necessary checks. Use the existing plan rather than rebuilding the architecture from scratch.
- Build the post-call workflow first, then live monitoring, then operational qualification. Keep live monitoring in the planned scope.
- Use synthetic data until real-data processing and retention policies are approved. Never put customer recordings, raw transcripts, credentials or sensitive fixtures in Git.
- Do not send coaching notes, alerts or other messages to external recipients unless the user has authorised that action. Saving an in-app note is distinct from sending a message.
- Do not create or push remote repositories or deploy without task authorisation.
- Do not spawn subagents unless the user explicitly requests delegation. The current user explicitly requested subagent-driven execution with GPT-6 Luna coding and GPT-6 Sol review; follow that per-task sequence for this implementation. Native sequential implementation is the default for later tasks unless the user changes this direction.

## Implementation approach

Inspect existing files and callers before changing code. Reuse existing helpers and dependencies, then standard-library and native platform features. Add the smallest correct change at the shared source of a problem.

Proposed stack: Python 3.12+, FastAPI/Pydantic, psycopg/PostgreSQL, private object storage, locally served Whisper speech recognition, locally served audit LLM, and React/TypeScript. Read `docs/open-source-models.md`; confirm licenses, compatibility and pin code plus model artifact revisions when implementing. Do not install every technology mentioned in the article.

Keep one backend codebase with API and worker processes. PostgreSQL holds transactional state, job leases and final event records. Audio belongs in private object storage. Use SSE for the browser's one-way live feed and the selected vendor's media protocol for audio ingestion.

Run speech recognition, diarization when needed, redaction, sentiment and audit inference on infrastructure controlled by this project. Do not route call audio or transcripts to hosted STT/LLM APIs. A hosted telephony platform may still deliver media, but AI processing stays self-hosted. Model artifacts may be downloaded during controlled provisioning, pinned by revision/checksum and served offline; do not fetch mutable model names at runtime. Gate live release on measured local latency and capacity.

Do not add Kafka, Kubernetes, Temporal, a second database, a generic provider framework or custom model training without measured need and an explicit scoped change. Record a real simplifying ceiling with a short `ponytail:` comment when one is introduced.

Use focused modules and explicit typed contracts. Avoid unrelated refactoring and speculative abstractions. Preserve user edits. Never delete data, reset changes or overwrite unrelated work as a cleanup step.

## Non-negotiable domain behaviour

- UTC timestamps; integer millisecond call offsets; finite validated numbers.
- Organisation scope on persisted call data, queries, composite references and event subscriptions.
- Identity, team membership and permissions derived server-side. A submitted call ID or agent ID is not authorisation.
- Media content, duration, codec, size and payload validation at intake; bounded queues and decoder resources.
- Partial transcripts are replaceable and provisional. Stable finals alone support durable audit evidence. Late corrections create transcript/audit revisions.
- Speaker labels are AGENT, CUSTOMER, UNKNOWN or IVR; never assume the first speaker is the agent.
- Evaluate disclosure only after its eligible opportunity window; unknown role, missing audio or uncertain timing must remain unknown/reviewable.
- Redact before LLM submission, browser publication or ordinary persistence/logging. Do not retain original secrets in redaction metadata. Redaction failure blocks the affected path.
- Validate LLM output at runtime, including all seven dimension IDs and canonical evidence references. Do not use Python `assert` to validate untrusted production inputs.
- Compute weighted scores and decisions on the server. Missing required evidence produces NEEDS_REVIEW, not an invented PASS or FAIL.
- Keep configured dispositions separate from QA scores. A local model proposes validated typed semantic signals; tenant-scoped immutable JSON configuration and deterministic rules resolve the outcome. Model confidence is untrusted until calibrated on adjudicated data.
- Keep machine audits and human reviews as separate immutable revisions. Require a reason and optimistic concurrency for overrides.
- Make local inference retries bounded, job effects idempotent, leases recoverable and stale commits impossible.
- Show degraded/stale/incomplete state in the UI; disconnected monitoring cannot imply all-clear.
- Tombstones block access and new processing before asynchronous deletion. Holds, object versions, backups and restored data follow the approved lifecycle.

## Verification

For documentation changes, check relative links, path references, requirement/task coverage and consistency. Do not run nonexistent application commands or report proposed checks as passed.

For nontrivial implementation, leave the smallest meaningful runnable regression check. Add integration checks for persistence, authorisation, leases, retries and deletion races; browser checks for the critical review and reconnect flows. Use deterministic local inference fakes in ordinary CI. Run pinned-model quality and load checks in isolated staging on the selected hardware.

Planned commands become valid only after the corresponding files exist:

```powershell
python -m unittest discover -s tests -v
npm --prefix web run build
npm --prefix web run test:e2e
```

Run integration tests only against an isolated database whose name ends in `_test`. Never clear or recreate a production/shared database to make a test pass.

Test the failure path relevant to the change: duplicate intake, unknown speakers, partial-to-final replacement, invented evidence, tenant isolation, conflicting reviews, inference failure, stale feeds or deletion during processing. Broaden testing when new changes or unresolved failures justify it.

Before claiming completion, state what changed, what actually ran and any unresolved limitations. No production-readiness claims without the operations guide's quality, privacy, restore, load and cost evidence.

## Documentation and delivery

Update contracts and operational instructions when behaviour changes. Distinguish proposed targets from measurements and examples from approved policy. Preserve prompt/model/ruleset/rubric/transcript version lineage.

Write concise, concrete progress updates. Ask for missing information only when it affects a necessary decision; continue independent authorised work. At handoff, link changed documents or files and report verification honestly.
