CREATE TABLE IF NOT EXISTS identity_memberships (
  subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
  organisation_id uuid NOT NULL REFERENCES organisations(id),
  user_id text NOT NULL CHECK (length(btrim(user_id)) > 0),
  role text NOT NULL CHECK (role IN ('AGENT','TEAM_LEADER','QA_ANALYST','COMPLIANCE_OFFICER','ADMIN')),
  team_id text,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (team_id IS NULL OR length(btrim(team_id)) > 0),
  CHECK (role NOT IN ('AGENT','TEAM_LEADER') OR team_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS identity_memberships_subject_idx
  ON identity_memberships(subject, organisation_id) WHERE is_active;
