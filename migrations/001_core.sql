CREATE TABLE IF NOT EXISTS organisations (
  id uuid PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS calls (
  organisation_id uuid NOT NULL REFERENCES organisations(id),
  id uuid NOT NULL,
  external_ref text NOT NULL,
  idempotency_key text NOT NULL,
  payload_sha256 char(64) NOT NULL,
  agent_id text NOT NULL,
  team_id text NOT NULL,
  language text NOT NULL,
  processing_state text NOT NULL CHECK (processing_state IN ('QUEUED','TRANSCRIBING','ANALYSING','AUDITING','READY','RETRY_WAIT','NEEDS_REVIEW','FAILED','DELETING','DELETED')),
  tombstoned_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, id),
  UNIQUE (organisation_id, external_ref),
  UNIQUE (organisation_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS audio_objects (
  organisation_id uuid NOT NULL,
  id uuid NOT NULL,
  call_id uuid NOT NULL,
  private_key text NOT NULL UNIQUE,
  checksum char(64) NOT NULL,
  codec text NOT NULL,
  sample_rate integer NOT NULL,
  channels smallint NOT NULL,
  duration_ms integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, id),
  FOREIGN KEY (organisation_id, call_id) REFERENCES calls(organisation_id, id)
);
CREATE TABLE IF NOT EXISTS jobs (
  organisation_id uuid NOT NULL,
  id uuid NOT NULL,
  call_id uuid NOT NULL,
  stage text NOT NULL,
  input_revision integer NOT NULL DEFAULT 1,
  state text NOT NULL CHECK (state IN ('QUEUED','RUNNING','DONE','RETRY_WAIT','FAILED')),
  attempts integer NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL DEFAULT now(),
  lease_token uuid,
  lease_until timestamptz,
  last_error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, id),
  FOREIGN KEY (organisation_id, call_id) REFERENCES calls(organisation_id, id),
  UNIQUE (organisation_id, call_id, stage, input_revision)
);
CREATE INDEX IF NOT EXISTS jobs_due_idx ON jobs(state, available_at, lease_until);
CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
