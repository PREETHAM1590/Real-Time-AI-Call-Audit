# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

QA analysts and supervisors need to review contact-centre calls and understand evidence-backed quality and outcome findings. Agents may upload recordings manually when assigned to one team. Operations staff configure later telephony connections.

## Product Purpose

Provide a stable call-audit platform that turns recordings into redacted transcripts, configurable disposition outcomes and reviewable quality findings. Success means a reviewer can inspect the evidence, understand uncertainty and record a reasoned human decision.

## Positioning

Self-hosted speech and language inference with deterministic, versioned disposition resolution and human review. Telephony integrations are provider-specific adapters; manual WAV/MP3 upload is available while live providers are being connected.

## Operating Context

The current web pilot is an analyst queue and call detail review surface. Reviewers work with final redacted transcript evidence, audit dimensions, policy findings, disposition results and permission-gated audio playback. Upload and telephony setup depend on server-side organisation, role and team assignments.

## Capabilities and Constraints

- Existing browser code is dependency-free HTML, CSS and JavaScript served by FastAPI.
- Manual WAV/MP3 recording upload is implemented for an AGENT with exactly one server-resolved team; the limit is 250 MiB.
- Call audio, transcripts and model inference must remain on project-controlled infrastructure; no hosted AI API fallback.
- Provider support must be stated per verified capability (live media, post-call recordings or events). India provider coverage is not yet exhaustive, provider choice is undecided, and individual protocols, account entitlements and credentials require validation.
- The provider coverage page distinguishes vendor-documented capabilities from project qualification. MyOperator's API docs state that recording links remain valid for 24 hours; tenant entitlements and runtime behavior still require qualification.
- The review queue exposes its server-side decision/age ordering plus call age and assigned agent/team context. Audit evidence controls show and highlight their validated quote in the canonical transcript.
- Use synthetic recordings until real-data processing and retention policies are approved.
- Preserve the current API contracts and server-side authorisation, transcript redaction, evidence, review and audio-access safeguards.

## Evidence on Hand

The repository contains a proposed specification, implementation plan, operations and evaluation guide, self-hosted model guide, and a working analyst review pilot. No deployment, provider account credentials, production model approval, representative real-data benchmark or production readiness evidence exists.

## Product Principles

- Keep human reviewers accountable for consequential findings.
- Make evidence, provenance, uncertainty and stale state visible.
- Protect tenant boundaries and sensitive audio/transcript data.
- Keep models self-hosted and replaceable; keep provider adapters explicit.
- Treat measured evidence as the basis for release claims.

## Accessibility & Inclusion

Use semantic HTML, keyboard-operable controls, visible focus, descriptive status/error messages, and responsive layouts. Respect reduced-motion preferences.
