CREATE TABLE IF NOT EXISTS disposition_config_versions (
  organisation_id uuid NOT NULL,
  config_id text NOT NULL CHECK (config_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$'),
  use_case_id text NOT NULL CHECK (use_case_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$'),
  version integer NOT NULL CHECK (version > 0),
  content_hash char(64) NOT NULL,
  schema_version text NOT NULL,
  raw_json jsonb NOT NULL,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, config_id, version),
  UNIQUE (organisation_id, config_id, version, content_hash),
  FOREIGN KEY (organisation_id) REFERENCES organisations(id)
);

-- ponytail: Keep one org-wide active config until authoritative calls carry a use-case routing key; upgrade to (organisation_id,use_case_id) when routing and membership selection are approved.
CREATE TABLE IF NOT EXISTS active_disposition_configs (
  organisation_id uuid PRIMARY KEY REFERENCES organisations(id),
  config_id text NOT NULL,
  version integer NOT NULL,
  generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
  changed_by text NOT NULL,
  changed_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (organisation_id, config_id, version)
    REFERENCES disposition_config_versions(organisation_id, config_id, version)
);

CREATE TABLE IF NOT EXISTS disposition_config_events (
  organisation_id uuid NOT NULL REFERENCES organisations(id),
  id uuid NOT NULL,
  actor_id text NOT NULL,
  action text NOT NULL CHECK (action IN ('STAGED','ACTIVATED','ROLLBACK','REPLAYED')),
  config_id text NOT NULL,
  version integer NOT NULL,
  reason text,
  request_id text,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, id),
  FOREIGN KEY (organisation_id, config_id, version)
    REFERENCES disposition_config_versions(organisation_id, config_id, version)
);

CREATE TABLE IF NOT EXISTS dispositions (
  organisation_id uuid NOT NULL,
  id uuid NOT NULL,
  call_id uuid NOT NULL,
  revision integer NOT NULL CHECK (revision > 0),
  transcript_revision integer NOT NULL CHECK (transcript_revision > 0),
  config_id text NOT NULL,
  config_version integer NOT NULL,
  config_hash char(64) NOT NULL,
  schema_version text NOT NULL,
  model_artifact text NOT NULL,
  adapter_version text NOT NULL,
  processing_path text NOT NULL CHECK (processing_path IN ('AUTHORITATIVE_FRONT_GATE','SINGLE_PASS','TURN_AWARE_MAP_AGGREGATE','ABSTAIN')),
  status text NOT NULL CHECK (status IN ('RESOLVED','NEEDS_REVIEW')),
  code text,
  parent_code text,
  confidence double precision CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  requires_review boolean NOT NULL,
  review_reason text,
  matched_rule_id text,
  signals_json jsonb NOT NULL,
  usage_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, id),
  UNIQUE (organisation_id, call_id, revision),
  UNIQUE (organisation_id, call_id, transcript_revision, config_id, config_version, model_artifact, adapter_version),
  FOREIGN KEY (organisation_id, call_id) REFERENCES calls(organisation_id, id),
  FOREIGN KEY (organisation_id, config_id, config_version, config_hash)
    REFERENCES disposition_config_versions(organisation_id, config_id, version, content_hash),
  CHECK ((status = 'RESOLVED' AND code IS NOT NULL AND NOT requires_review) OR
         (status = 'NEEDS_REVIEW' AND code IS NULL AND requires_review))
);

CREATE INDEX IF NOT EXISTS dispositions_call_current_idx
  ON dispositions (organisation_id, call_id, revision DESC);

CREATE OR REPLACE FUNCTION reject_disposition_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'disposition history is immutable';
END;
$$;

DROP TRIGGER IF EXISTS disposition_config_versions_immutable ON disposition_config_versions;
CREATE TRIGGER disposition_config_versions_immutable BEFORE UPDATE OR DELETE ON disposition_config_versions
  FOR EACH ROW EXECUTE FUNCTION reject_disposition_mutation();
DROP TRIGGER IF EXISTS dispositions_immutable ON dispositions;
CREATE TRIGGER dispositions_immutable BEFORE UPDATE OR DELETE ON dispositions
  FOR EACH ROW EXECUTE FUNCTION reject_disposition_mutation();
DROP TRIGGER IF EXISTS disposition_config_events_immutable ON disposition_config_events;
CREATE TRIGGER disposition_config_events_immutable BEFORE UPDATE OR DELETE ON disposition_config_events
  FOR EACH ROW EXECUTE FUNCTION reject_disposition_mutation();
