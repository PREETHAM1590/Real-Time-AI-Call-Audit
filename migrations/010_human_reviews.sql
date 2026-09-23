ALTER TABLE audits
  ADD COLUMN pass_threshold numeric(3,2)
    CHECK (pass_threshold IS NULL OR pass_threshold BETWEEN 1 AND 5);

ALTER TABLE audits
  ADD CONSTRAINT audits_scope_reference_key UNIQUE (organisation_id,id,call_id);

CREATE TABLE reviews (
  organisation_id uuid NOT NULL,
  id uuid NOT NULL,
  audit_id uuid NOT NULL,
  call_id uuid NOT NULL,
  audit_revision integer NOT NULL CHECK (audit_revision > 0),
  version integer NOT NULL CHECK (version > 0),
  base_review_version integer NOT NULL CHECK (base_review_version >= 0 AND version = base_review_version + 1),
  reviewer_id text NOT NULL CHECK (length(reviewer_id) BETWEEN 1 AND 255),
  action text NOT NULL CHECK (action IN ('ACCEPT','OVERRIDE','TRIAGE')),
  changed_scores_json jsonb NOT NULL CHECK (jsonb_typeof(changed_scores_json) = 'object'),
  effective_scores_json jsonb NOT NULL CHECK (jsonb_typeof(effective_scores_json) = 'object'),
  effective_score numeric(4,2) CHECK (effective_score IS NULL OR effective_score BETWEEN 1 AND 5),
  effective_decision text NOT NULL CHECK (effective_decision IN ('PASS','FAIL','NEEDS_REVIEW')),
  reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id,id),
  UNIQUE (organisation_id,audit_id,version),
  FOREIGN KEY (organisation_id,audit_id,call_id) REFERENCES audits(organisation_id,id,call_id),
  FOREIGN KEY (organisation_id,call_id) REFERENCES calls(organisation_id,id),
  CHECK ((action IN ('ACCEPT','TRIAGE') AND changed_scores_json='{}'::jsonb) OR
         (action='OVERRIDE' AND changed_scores_json<>'{}'::jsonb)),
  CHECK (effective_score IS NOT NULL OR effective_decision='NEEDS_REVIEW')
);

CREATE INDEX reviews_audit_head_idx ON reviews(organisation_id,audit_id,version DESC);

CREATE TABLE access_events (
  organisation_id uuid NOT NULL REFERENCES organisations(id),
  id uuid NOT NULL,
  actor_id text NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 255),
  action text NOT NULL CHECK (action IN ('REVIEW_SAVED')),
  resource_type text NOT NULL CHECK (resource_type IN ('AUDIT_REVIEW')),
  resource_id uuid NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('SUCCESS')),
  request_id text CHECK (request_id IS NULL OR length(request_id) BETWEEN 1 AND 128),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (organisation_id,id)
);

CREATE OR REPLACE FUNCTION reject_review_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'review and access history is immutable';
END;
$$;

CREATE TRIGGER reviews_immutable BEFORE UPDATE OR DELETE ON reviews
  FOR EACH ROW EXECUTE FUNCTION reject_review_history_mutation();
CREATE TRIGGER access_events_immutable BEFORE UPDATE OR DELETE ON access_events
  FOR EACH ROW EXECUTE FUNCTION reject_review_history_mutation();
