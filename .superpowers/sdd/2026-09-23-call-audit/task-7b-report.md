# Task 7B implementation report

Implementation commit: `dfd18cf` (`feat: add authenticated Exotel recording intake`).
Review-fix commit: `17c7086` (`fix: bound and fence Exotel stream intake`).

Files in the implementation commit:

- `app/api.py`, `app/exotel_adapter.py`, `app/ingest.py`
- `migrations/016_exotel_integrations.sql`
- `tests/test_exotel_adapter.py`, `tests/test_exotel_api.py`
- `docs/system-specification.md`, `docs/superpowers/plans/2026-09-23-call-audit.md`, `docs/operations-and-evaluation.md`

ADMINs can create, list and disable tenant-scoped Exotel integrations and manage mappings to an active AGENT with exactly one active team membership. Creation returns a generated Basic username/password once; the database stores a salted scrypt verifier, and audit events contain no credentials. The WSS route checks Basic auth and the integration's account SID, rejects unmapped or inactive agent identities, validates connected/start/media/stop with `ExotelSession`, rejects sequence/chunk/timestamp gaps and bounds sessions to eight connections per API worker, 100 MB PCM and two hours. A 2 MiB spooled-file threshold rolls larger input to temporary storage. Clean stop builds an 8 kHz mono WAV and calls ordinary durable intake. SHA-256 references are organisation/account/call scoped; persisted generation fencing uses the hash key and locks the session/integration rows through the intake transaction. Caller `from`/`to` values are parsed but not retained.

Checks actually run:

- `python -m unittest tests.test_exotel tests.test_exotel_adapter tests.test_exotel_api tests.test_ingest -v` — 26 tests ran, 23 passed and 3 PostgreSQL tests skipped because no isolated `_test` database was configured.
- `python -m unittest discover -s tests -v` — 183 ran, all passed, 37 PostgreSQL integration tests skipped because no isolated `_test` database was configured.
- `git diff --check` — passed.
- Relative Markdown links in the system specification, implementation plan and operations guide — passed.

No PostgreSQL migration, CRUD transaction, generation-race or durable-intake integration test ran without an explicitly configured isolated `_test` database. The WebSocket checks use only synthetic frames and a mocked database/intake. Unit mocks verify final intake rejects revoked membership before any insert, the authentication admission cap rejects before database work, timeouts close idle/overlong sockets, and migration SQL installs an update/delete rejection trigger. There is no Exotel account, so WSS networking, account entitlement, callflow, agent-leg coverage and provider qualification remain unverified. This task does not add live STT, sentiment, alerts, a supervisor view, two-leg diarization or crash-resumable partial streams. The connection limit is per API worker; a global deployment limit would need shared admission control if measured capacity requires one.

Self-review: the WebSocket path cannot enqueue from partial/disconnected streams; generation, integration, current agent mapping and active memberships are rechecked and locked in a serializable database transaction with ordinary intake, so newer sessions, disabled integrations, mapping changes and identity revocation cannot race a stale commit. Persisted provider call references are hashes, not raw CallSIDs. Admin CRUD/migration behavior still needs isolated PostgreSQL integration coverage, and actual Exotel callflow behavior remains an operational qualification step.
