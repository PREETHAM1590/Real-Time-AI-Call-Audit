-- Preserve distinct immutable audit revisions when the prompt/runtime changes
-- even if model and content checksums happen to remain identical.
ALTER TABLE audits
  DROP CONSTRAINT IF EXISTS audits_organisation_id_call_id_transcript_revision_model_ar_key;

ALTER TABLE audits
  ADD CONSTRAINT audits_identity_versions_key UNIQUE (
    organisation_id,
    call_id,
    transcript_revision,
    model_artifact,
    inference_runtime,
    prompt_version,
    prompt_hash,
    rubric_hash,
    policy_fingerprint
  );

CREATE OR REPLACE FUNCTION reject_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit history is immutable';
END;
$$;

DROP TRIGGER IF EXISTS audits_immutable ON audits;
CREATE TRIGGER audits_immutable BEFORE UPDATE OR DELETE ON audits
  FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation();
