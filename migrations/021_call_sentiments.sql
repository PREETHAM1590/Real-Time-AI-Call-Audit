-- Advisory, uncalibrated per-call sentiment output from the local sentiment adapter
-- (app/sentiment_adapter.py, app/sentiment.py). Never used to pass/fail a call and
-- never overwrites calls.processing_state; see app/worker.py's SENTIMENT stage and
-- app/ingest.py's finish_job "sentiment" branch.
CREATE TABLE IF NOT EXISTS call_sentiments (
  organisation_id uuid NOT NULL,
  id uuid NOT NULL,
  call_id uuid NOT NULL,
  revision integer NOT NULL CHECK (revision > 0),
  transcript_revision integer NOT NULL CHECK (transcript_revision > 0),
  model_artifact text NOT NULL,
  adapter_version text NOT NULL,
  status text NOT NULL CHECK (status IN ('OK', 'UNKNOWN', 'UNAVAILABLE')),
  -- Per-utterance objects carry ONLY id, start_ms, end_ms, signed_score and
  -- top_class_probability. No utterance text is ever persisted here.
  signals_json jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(signals_json) = 'array'),
  alert_offsets_ms jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(alert_offsets_ms) = 'array'),
  trend_json jsonb,
  failed_utterance_count integer NOT NULL DEFAULT 0 CHECK (failed_utterance_count >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, id),
  UNIQUE (organisation_id, call_id, revision),
  UNIQUE (organisation_id, call_id, transcript_revision, model_artifact, adapter_version),
  FOREIGN KEY (organisation_id, call_id) REFERENCES calls(organisation_id, id)
);

CREATE INDEX IF NOT EXISTS call_sentiments_call_current_idx
  ON call_sentiments (organisation_id, call_id, transcript_revision DESC, revision DESC);

CREATE OR REPLACE FUNCTION reject_call_sentiment_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'call_sentiments history is immutable';
END;
$$;

-- Immutable like other machine-inference tables, but DELETE stays allowed (unlike
-- audits/dispositions/reviews) so app.retention.purge_call can remove advisory
-- sentiment rows for a tombstoned call the same way it removes findings.
DROP TRIGGER IF EXISTS call_sentiments_immutable ON call_sentiments;
CREATE TRIGGER call_sentiments_immutable BEFORE UPDATE ON call_sentiments
  FOR EACH ROW EXECUTE FUNCTION reject_call_sentiment_update();
