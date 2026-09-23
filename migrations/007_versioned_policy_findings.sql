ALTER TABLE calls
  ADD COLUMN IF NOT EXISTS agent_connected_ms integer CHECK (agent_connected_ms IS NULL OR agent_connected_ms >= 0),
  ADD COLUMN IF NOT EXISTS tagged_intervals jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(tagged_intervals) = 'array'),
  ADD COLUMN IF NOT EXISTS call_complete boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS timing_reliable boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS call_type text NOT NULL DEFAULT 'unknown' CHECK (length(call_type) BETWEEN 1 AND 128);

CREATE TABLE IF NOT EXISTS findings (
  organisation_id uuid NOT NULL,
  id uuid NOT NULL,
  call_id uuid NOT NULL,
  transcript_revision integer NOT NULL CHECK (transcript_revision > 0),
  rule_id text NOT NULL CHECK (length(rule_id) BETWEEN 2 AND 128),
  ruleset_version text NOT NULL CHECK (length(ruleset_version) BETWEEN 1 AND 64),
  ruleset_hash char(64) NOT NULL CHECK (ruleset_hash ~ '^[0-9a-f]{64}$'),
  policy_text_version text NOT NULL CHECK (length(policy_text_version) BETWEEN 1 AND 128),
  status text NOT NULL CHECK (status IN ('PENDING','SATISFIED','POTENTIAL_VIOLATION','UNKNOWN','ADVISORY')),
  severity text NOT NULL CHECK (severity IN ('LOW','MEDIUM','HIGH')),
  evidence_ids jsonb NOT NULL CHECK (jsonb_typeof(evidence_ids) = 'array'),
  evidence_fingerprint char(64) NOT NULL CHECK (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
  deadline_ms integer CHECK (deadline_ms IS NULL OR deadline_ms >= 0),
  remediation text NOT NULL CHECK (length(remediation) BETWEEN 1 AND 500),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, id),
  UNIQUE (organisation_id, call_id, transcript_revision, ruleset_hash, rule_id, evidence_fingerprint),
  FOREIGN KEY (organisation_id, call_id) REFERENCES calls(organisation_id, id)
);

CREATE INDEX IF NOT EXISTS findings_call_current_idx
  ON findings (organisation_id, call_id, transcript_revision DESC, created_at);
