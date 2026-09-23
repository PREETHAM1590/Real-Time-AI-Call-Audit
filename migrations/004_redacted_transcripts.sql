ALTER TABLE calls
  ADD COLUMN IF NOT EXISTS transcript_revision integer NOT NULL DEFAULT 0
  CHECK (transcript_revision >= 0);

CREATE TABLE IF NOT EXISTS transcript_utterances (
  organisation_id uuid NOT NULL,
  call_id uuid NOT NULL,
  revision integer NOT NULL CHECK (revision > 0),
  id text NOT NULL,
  segment_id text NOT NULL,
  speaker_id text NOT NULL,
  role text NOT NULL CHECK (role IN ('AGENT','CUSTOMER','UNKNOWN','IVR')),
  start_ms integer NOT NULL CHECK (start_ms >= 0),
  end_ms integer NOT NULL CHECK (end_ms >= start_ms),
  text_redacted text NOT NULL CHECK (length(text_redacted) <= 10000),
  confidence double precision CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  model_version text NOT NULL,
  is_final boolean NOT NULL DEFAULT true CHECK (is_final),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, call_id, revision, id),
  UNIQUE (organisation_id, call_id, revision, segment_id),
  FOREIGN KEY (organisation_id, call_id) REFERENCES calls(organisation_id, id)
);

CREATE INDEX IF NOT EXISTS transcript_utterances_call_idx
  ON transcript_utterances (organisation_id, call_id, revision, start_ms, id);
