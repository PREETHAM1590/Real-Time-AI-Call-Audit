# Configurable Disposition Intelligence Platform - Design Package

This package defines a tenant-neutral, use-case-neutral platform where business behavior is supplied through versioned JSON configuration.

## Deliverables

- `PRD_Configurable_Disposition_Platform.docx` - product requirements, scope, journeys, governance, success metrics, acceptance criteria.
- `TRD_Configurable_Disposition_Platform.docx` - technical requirements, interfaces, DSL, reliability, scaling, security, testing.
- `System_Design_Configurable_Disposition_Platform.docx` - architecture, component design, adaptive transcript routing, storage, deployment and design decisions.
- `JSON_Configuration_Guide.docx` - practical authoring guide for onboarding new tenants/use cases through configuration only.
- `config.template.json` - validated starter configuration.
- `config.schema.json` - JSON Schema (Draft 2020-12) for structural validation.

## Core design contract

1. Business semantics and naming live in configuration.
2. Typed model questions produce reusable semantic signals.
3. Deterministic rules produce the final configured disposition.
4. Normal transcripts use a single evaluation request; oversized transcripts automatically use a turn-aware map/aggregate fallback.
5. Configuration is validated, compiled, versioned, replay-tested, staged, activated and rollbackable without changing runtime code.
