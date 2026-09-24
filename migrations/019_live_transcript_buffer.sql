ALTER TABLE exotel_sessions ADD COLUMN call_content_purged_at timestamptz;

CREATE TABLE live_transcript_utterances (
  organisation_id uuid NOT NULL,
  integration_id uuid NOT NULL,
  call_key char(64) NOT NULL,
  generation bigint NOT NULL CHECK (generation > 0),
  utterance_id text NOT NULL CHECK (utterance_id ~ '^[0-9a-f]{32}$'),
  start_ms integer NOT NULL CHECK (start_ms >= 0),
  end_ms integer NOT NULL CHECK (end_ms >= start_ms),
  role text NOT NULL CHECK (role IN ('AGENT','CUSTOMER','UNKNOWN','IVR')),
  text_redacted text NOT NULL CHECK (length(text_redacted) <= 1600),
  is_final boolean NOT NULL DEFAULT false CHECK (is_final = false),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (organisation_id, integration_id, call_key, generation, utterance_id),
  FOREIGN KEY (organisation_id, integration_id, call_key)
    REFERENCES exotel_sessions(organisation_id, integration_id, call_key) ON DELETE CASCADE
);
CREATE INDEX live_transcript_expiry_idx ON live_transcript_utterances(expires_at);
CREATE INDEX live_transcript_session_idx
  ON live_transcript_utterances(organisation_id, call_key, generation, start_ms, utterance_id);
