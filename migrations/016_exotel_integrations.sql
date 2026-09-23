CREATE TABLE exotel_integrations (
  organisation_id uuid NOT NULL REFERENCES organisations(id),
  id uuid NOT NULL,
  account_sid text NOT NULL CHECK (length(account_sid) BETWEEN 1 AND 256),
  username text NOT NULL UNIQUE CHECK (length(username) BETWEEN 1 AND 128),
  password_salt bytea NOT NULL CHECK (octet_length(password_salt) = 16),
  password_verifier bytea NOT NULL CHECK (octet_length(password_verifier) = 32),
  is_active boolean NOT NULL DEFAULT true,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  disabled_at timestamptz,
  PRIMARY KEY (organisation_id, id),
  UNIQUE (account_sid)
);

CREATE TABLE exotel_agent_mappings (
  organisation_id uuid NOT NULL,
  integration_id uuid NOT NULL,
  agent_ref text NOT NULL CHECK (length(agent_ref) BETWEEN 1 AND 128),
  agent_id text NOT NULL,
  team_id text NOT NULL,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, integration_id, agent_ref),
  FOREIGN KEY (organisation_id, integration_id) REFERENCES exotel_integrations(organisation_id, id)
);

CREATE TABLE exotel_integration_events (
  organisation_id uuid NOT NULL,
  integration_id uuid NOT NULL,
  actor_id text NOT NULL,
  action text NOT NULL CHECK (action IN ('CREATED','DISABLED','AGENT_MAPPED','AGENT_UNMAPPED')),
  subject_ref text,
  request_id text,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (organisation_id, integration_id) REFERENCES exotel_integrations(organisation_id, id)
);
CREATE INDEX exotel_integration_events_idx ON exotel_integration_events(organisation_id, integration_id, occurred_at);

CREATE TABLE exotel_sessions (
  organisation_id uuid NOT NULL,
  integration_id uuid NOT NULL,
  call_key char(64) NOT NULL CHECK (call_key ~ '^[0-9a-f]{64}$'),
  generation bigint NOT NULL CHECK (generation > 0),
  started_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id, integration_id, call_key),
  FOREIGN KEY (organisation_id, integration_id) REFERENCES exotel_integrations(organisation_id, id)
);
