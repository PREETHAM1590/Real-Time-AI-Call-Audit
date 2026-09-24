ALTER TABLE exotel_sessions
  ADD COLUMN state text NOT NULL DEFAULT 'UNKNOWN'
    CHECK (state IN ('UNKNOWN','LIVE','DRAINING','ENDED','INCOMPLETE')),
  ADD COLUMN agent_id text,
  ADD COLUMN team_id text,
  ADD COLUMN draining_at timestamptz,
  ADD COLUMN ended_at timestamptz,
  ADD COLUMN incomplete_at timestamptz,
  ADD COLUMN updated_at timestamptz,
  ADD COLUMN last_activity_at timestamptz;

UPDATE exotel_sessions SET updated_at=started_at,last_activity_at=started_at;

ALTER TABLE exotel_sessions
  ALTER COLUMN updated_at SET DEFAULT now(),
  ALTER COLUMN updated_at SET NOT NULL,
  ALTER COLUMN last_activity_at SET DEFAULT now(),
  ALTER COLUMN last_activity_at SET NOT NULL;

CREATE INDEX exotel_sessions_live_scope_idx
  ON exotel_sessions(organisation_id, state, team_id, updated_at DESC);
