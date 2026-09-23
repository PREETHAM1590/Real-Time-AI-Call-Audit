# Real-Time AI Call Audit

Self-hosted call auditing and configurable disposition platform: transcribe calls, check configured policies, identify interaction outcomes, propose evidence-backed quality scores, and support human review and live supervision.

**Status:** post-call implementation in progress. Identity-scoped APIs, durable recording intake, final redacted transcription, configurable disposition, versioned policy findings, immutable seven-dimension audits, append-only human reviews, a small analyst review page and scoped quality reports have runnable checks. Timed policy findings remain UNKNOWN without trusted telephony context. Live monitoring, production model approval, quality/capacity qualification and deployment remain incomplete. Nothing is deployed.

## Read in this order

1. [Product and technical specification](docs/system-specification.md) — requirements, architecture, contracts, privacy, and scope.
2. [Implementation plan](docs/superpowers/plans/2026-09-23-call-audit.md) — sequenced work, file ownership, checks, and release gates.
3. [Operations and evaluation guide](docs/operations-and-evaluation.md) — environments, quality evaluation, incidents, capacity, costs, and rollout.
4. [Agent instructions](AGENTS.md) — working conventions, privacy boundaries, implementation order, and verification requirements for coding agents.
5. [Open-source model guide](docs/open-source-models.md) — candidate local models, licenses, artifact controls, and live inference limits.
6. [Disposition design package](docs/Disposition_Platform_Design_Package/README.md) — user-provided product, technical and JSON configuration baseline; apply project self-hosting constraints where it conflicts.
7. [Agent briefing](AGENT.md) and [binding agent instructions](AGENTS.md).

## Delivery approach

Build a post-call pilot first, validate disposition and audit findings with QA analysts, then add live transcription and supervisor alerts using the same transcript, disposition and audit contracts. The live system remains part of the planned scope. Large-scale infrastructure is a separate expansion milestone driven by measured load.

The starting proposal uses Python/FastAPI, PostgreSQL, private object storage, self-hosted speech and language models, and React/TypeScript. The current analyst pilot uses dependency-free HTML/CSS/JavaScript because this repository had no existing browser application/build scaffold; that scoped implementation choice and its replacement path are recorded in the plan. Model candidates, licenses and deployment boundaries are documented in the [open-source model guide](docs/open-source-models.md).

## Source and assumptions

Based on the user-provided [call-audit architecture article](https://medium.com/@shashank_shekhar_pandey/building-a-real-time-ai-call-audit-system-speech-to-text-llm-evaluation-compliance-monitoring-585fb8d53be7), attributed in the pasted text to Shashank Shekhar Pandey, dated June 10, 2026, and a user-provided disposition design package. The references are engineering/product inputs, not verified implementations or legal specifications.

The article is an engineering reference, not a verified implementation or a legal specification. Performance, infrastructure costs, model quality, licenses and compliance requirements must be established for this project. The documentation explicitly identifies proposed defaults and later-stage features.
