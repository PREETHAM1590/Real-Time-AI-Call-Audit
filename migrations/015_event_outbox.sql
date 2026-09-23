CREATE TABLE event_counters (
  organisation_id uuid PRIMARY KEY REFERENCES organisations(id),
  last_sequence bigint NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
  oldest_sequence bigint NOT NULL DEFAULT 1 CHECK (oldest_sequence >= 1),
  CHECK (oldest_sequence <= last_sequence + 1)
);

CREATE TABLE events (
  organisation_id uuid NOT NULL,
  sequence bigint NOT NULL CHECK (sequence > 0),
  call_id uuid NOT NULL,
  type text NOT NULL CHECK (type = 'call.updated'),
  schema_version smallint NOT NULL DEFAULT 1 CHECK (schema_version = 1),
  occurred_at timestamptz NOT NULL DEFAULT now(),
  payload jsonb NOT NULL CHECK (
    jsonb_typeof(payload) = 'object'
    AND payload ? 'processing_state'
    AND payload ? 'transcript_revision'
    AND jsonb_typeof(payload->'processing_state') = 'string'
    AND jsonb_typeof(payload->'transcript_revision') = 'number'
    AND payload->>'transcript_revision' ~ '^(0|[1-9][0-9]*)$'
    AND (payload - ARRAY['processing_state','transcript_revision']) = '{}'::jsonb
  ),
  PRIMARY KEY (organisation_id, sequence),
  FOREIGN KEY (organisation_id, call_id) REFERENCES calls(organisation_id, id)
);

CREATE INDEX events_call_sequence_idx ON events(organisation_id, call_id, sequence);
