CREATE TABLE IF NOT EXISTS audits (
  organisation_id uuid NOT NULL,
  id uuid NOT NULL,
  call_id uuid NOT NULL,
  revision integer NOT NULL CHECK (revision > 0),
  transcript_revision integer NOT NULL CHECK (transcript_revision > 0),
  model_artifact text NOT NULL CHECK (length(model_artifact) BETWEEN 1 AND 128),
  inference_runtime text NOT NULL CHECK (length(inference_runtime) BETWEEN 1 AND 128),
  prompt_version text NOT NULL CHECK (length(prompt_version) BETWEEN 1 AND 128),
  prompt_hash char(64) NOT NULL CHECK (prompt_hash ~ '^[0-9a-f]{64}$'),
  rubric_version text NOT NULL CHECK (length(rubric_version) BETWEEN 1 AND 128),
  rubric_hash char(64) NOT NULL CHECK (rubric_hash ~ '^[0-9a-f]{64}$'),
  policy_provenance jsonb NOT NULL CHECK (jsonb_typeof(policy_provenance) = 'array'),
  policy_fingerprint char(64) NOT NULL CHECK (policy_fingerprint ~ '^[0-9a-f]{64}$'),
  dimensions_json jsonb NOT NULL CHECK (jsonb_typeof(dimensions_json) = 'array'),
  overall_score numeric(4,2) CHECK (overall_score IS NULL OR overall_score BETWEEN 1 AND 5),
  decision text NOT NULL CHECK (decision IN ('PASS','FAIL','NEEDS_REVIEW')),
  review_reason text CHECK (review_reason IS NULL OR length(review_reason) BETWEEN 1 AND 128),
  coaching_narrative jsonb NOT NULL CHECK (jsonb_typeof(coaching_narrative) = 'object'),
  highlights jsonb NOT NULL CHECK (jsonb_typeof(highlights) = 'array'),
  improvement_areas jsonb NOT NULL CHECK (jsonb_typeof(improvement_areas) = 'array'),
  usage_json jsonb NOT NULL CHECK (jsonb_typeof(usage_json) = 'object'),
  attempts smallint NOT NULL CHECK (attempts BETWEEN 0 AND 3),
  latency_ms integer NOT NULL CHECK (latency_ms >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, id),
  UNIQUE (organisation_id, call_id, revision),
  UNIQUE (organisation_id, call_id, transcript_revision, model_artifact, prompt_hash, rubric_hash, policy_fingerprint),
  FOREIGN KEY (organisation_id, call_id) REFERENCES calls(organisation_id, id)
);

CREATE INDEX IF NOT EXISTS audits_call_current_idx
  ON audits (organisation_id, call_id, transcript_revision DESC, revision DESC);
