ALTER TABLE exotel_sessions
  ADD COLUMN live_transcription_state text NOT NULL DEFAULT 'DISABLED'
    CHECK (live_transcription_state IN ('DISABLED','EMPTY','LIVE','DEGRADED')),
  ADD COLUMN live_transcript_truncated boolean NOT NULL DEFAULT false;
