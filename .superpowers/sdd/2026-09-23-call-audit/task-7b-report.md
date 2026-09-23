# Task 7B implementation report

Implementation commit: `dfd18cf` (`feat: add authenticated Exotel recording intake`).

Files in the implementation commit:

- `app/api.py`, `app/exotel_adapter.py`, `app/ingest.py`
- `migrations/016_exotel_integrations.sql`
- `tests/test_exotel_adapter.py`, `tests/test_exotel_api.py`
- `docs/system-specification.md`, `docs/superpowers/plans/2026-09-23-call-audit.md`, `docs/operations-and-evaluation.md`

ADMINs can create, list and disable tenant-scoped Exotel integrations and manage mappings to an active AGENT with exactly one active team membership. Creation returns a generated Basic username/password once; the database stores a salted scrypt verifier, and audit events contain no credentials. The WSS route checks Basic auth and the integration's account SID, rejects unmapped or inactive agent identities, validates connected/start/media/stop with `ExotelSession`, rejects sequence/chunk/timestamp gaps and bounds sessions to eight connections per API worker, 100 MB PCM and two hours. A 2 MiB spooled-file threshold rolls larger input to temporary storage. Clean stop builds an 8 kHz mono WAV and calls ordinary durable intake. SHA-256 references are organisation/account/call scoped; persisted generation fencing uses the hash key and locks the session/integration rows through the intake transaction. Caller `from`/`to` values are parsed but not retained.

Checks actually run:

- `python -m unittest tests.test_exotel tests.test_exotel_adapter tests.test_exotel_api -v` — 13 passed.
- `python -m unittest tests.test_ingest -v` — 6 passed, 3 PostgreSQL tests skipped because no isolated `_test` database was configured.
- `python -m unittest discover -s tests -v` — 179 ran, 1 failed, 37 skipped. The failure was `test_browser_review.AnalystBrowserSmokeTests.test_manual_upload_multipart_csrf_queue_and_retry_key`, an unrelated browser upload-status assertion in pre-existing dirty UI/test files. No Task 7B test failed.
- `git diff --cached --check` — passed for the implementation commit.
- Relative Markdown links in the system specification, implementation plan and operations guide — passed.

No PostgreSQL migration, CRUD transaction, generation-race or durable-intake integration test ran without an explicitly configured isolated `_test` database. The WebSocket checks use only synthetic frames and a mocked database/intake. There is no Exotel account, so WSS networking, account entitlement, callflow, agent-leg coverage and provider qualification remain unverified. This task does not add live STT, sentiment, alerts, a supervisor view, two-leg diarization or crash-resumable partial streams. The connection limit is per API worker; a global deployment limit would need shared admission control if measured capacity requires one.

Self-review: the WebSocket path cannot enqueue from partial/disconnected streams; generation checks share a database transaction with the existing idempotent intake, so a newer connection or integration disable fences an older stream before commit. Persisted provider call references are hashes, not raw CallSIDs. Admin CRUD/migration behavior still needs isolated PostgreSQL integration coverage, and actual Exotel callflow behavior remains an operational qualification step.
