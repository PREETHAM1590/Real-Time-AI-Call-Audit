# Agent briefing

## Product objective

Build a stable, multi-tenant call-audit platform with self-hosted inference. The post-call workflow comes first: validated audio intake, durable processing, final redacted transcript, policy checks, configurable disposition, evidence-backed QA score, and human review. Live monitoring follows on the same versioned transcript/results contracts.

## Source of truth

Read [`AGENTS.md`](AGENTS.md) first, then [`README.md`](README.md), [`docs/system-specification.md`](docs/system-specification.md), [`docs/superpowers/plans/2026-09-23-call-audit.md`](docs/superpowers/plans/2026-09-23-call-audit.md), [`docs/operations-and-evaluation.md`](docs/operations-and-evaluation.md), and [`docs/open-source-models.md`](docs/open-source-models.md). The user-provided [`disposition design package`](docs/Disposition_Platform_Design_Package/README.md) informs configurable disposition behavior; where its hosted Jev example conflicts with project policy, use a self-hosted local model behind the replaceable typed-signal adapter.

## Architecture boundaries

Keep a single Python backend with API and worker processes, PostgreSQL for scoped transactional state/jobs, private object storage for audio, and a React client. Build post-call first, then live monitoring. Use redacted final evidence for disposition and audit inference. A disposition is a separate immutable outcome from the seven-dimension QA score. The model proposes typed semantic signals; deterministic, versioned configuration resolves the final taxonomy code. Keep all model inference self-hosted and all model artifacts pinned.

Do not add Kafka, Kubernetes, Temporal, another database, arbitrary tenant code, external AI fallbacks, or speculative abstractions without measured need and an explicit scoped plan change. Use synthetic data until real-data and retention policies are approved.

## Work and verification

Execute the approved plan task-by-task. Coding uses GPT-6 Luna and independent review uses GPT-6 Sol, as requested by the user. Never claim a test, integration, model, deployment, latency, quality score, or production readiness that was not run and verified. See `AGENTS.md` for domain invariants, privacy rules, testing gates and recovery requirements.