# Real-Time AI Call Audit

Project documentation and implementation plan for a system that transcribes customer calls, checks configured policies, proposes evidence-backed quality scores, and supports human review and live supervision.

**Status:** planning only; no application has been implemented or deployed.

## Read in this order

1. [Product and technical specification](docs/system-specification.md) — requirements, architecture, contracts, privacy, and scope.
2. [Implementation plan](docs/superpowers/plans/2026-09-23-call-audit.md) — sequenced work, file ownership, checks, and release gates.
3. [Operations and evaluation guide](docs/operations-and-evaluation.md) — environments, quality evaluation, incidents, capacity, costs, and rollout.
4. [Agent instructions](AGENTS.md) — working conventions, privacy boundaries, implementation order, and verification requirements for coding agents.
5. [Open-source model guide](docs/open-source-models.md) — candidate local models, licenses, artifact controls, and live inference limits.

## Delivery approach

Build a post-call pilot first, validate its findings with QA analysts, then add live transcription and supervisor alerts using the same transcript and audit contracts. The live system remains part of the planned scope. Large-scale infrastructure is a separate expansion milestone driven by measured load.

The starting proposal uses Python/FastAPI, PostgreSQL, private object storage, self-hosted speech and language models, and React/TypeScript. Model candidates, licenses and deployment boundaries are documented in the [open-source model guide](docs/open-source-models.md). These are proposed choices for a new project, not dependencies found in this workspace.

## Source and assumptions

Based on the user-provided article **“Building a Real-Time AI Call Audit System: Speech-to-Text, LLM Evaluation, Compliance Monitoring, and Agent Scoring”**, attributed in the pasted text to Shashank Shekhar pandey, dated June 10, 2026. The attachment includes site navigation and promotional text; those are excluded from the product scope. Its interview advice is background material, not a product requirement.

The article is an engineering reference, not a verified implementation or a legal specification. Performance, infrastructure costs, model quality, licenses and compliance requirements must be established for this project. The documentation explicitly identifies proposed defaults and later-stage features.
