ALTER TABLE exotel_sessions
  ADD COLUMN state text NOT NULL DEFAULT 'INCOMPLETE'
    CHECK (state IN ('LIVE','DRAINING','ENDED','INCOMPLETE')),
  ADD COLUMN agent_id text,
  ADD COLUMN team_id text,
  ADD COLUMN draining_at timestamptz,
  ADD COLUMN ended_at timestamptz,
  ADD COLUMN incomplete_at timestamptz,
  ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN last_activity_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX exotel_sessions_live_scope_idx
  ON exotel_sessions(organisation_id, state, team_id, updated_at DESC);
