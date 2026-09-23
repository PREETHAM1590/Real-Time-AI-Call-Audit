ALTER TABLE calls
  ADD COLUMN retention_expires_at timestamptz,
  ADD COLUMN legal_hold boolean NOT NULL DEFAULT false,
  ADD COLUMN deletion_requested_at timestamptz,
  ADD COLUMN deletion_requested_by text CHECK (deletion_requested_by IS NULL OR length(deletion_requested_by) BETWEEN 1 AND 255),
  ADD COLUMN content_purged_at timestamptz;

ALTER TABLE access_events DROP CONSTRAINT access_events_action_check;
ALTER TABLE access_events ADD CONSTRAINT access_events_action_check
  CHECK (action IN ('REVIEW_SAVED','AUDIO_ACCESS_GRANTED','FINDINGS_EXPORTED','CALL_DELETION_REQUESTED','CALL_CONTENT_PURGED'));

ALTER TABLE access_events DROP CONSTRAINT access_events_resource_type_check;
ALTER TABLE access_events ADD CONSTRAINT access_events_resource_type_check
  CHECK (resource_type IN ('AUDIT_REVIEW','AUDIO_OBJECT','FINDINGS_EXPORT','CALL'));

CREATE INDEX calls_retention_due_idx ON calls(retention_expires_at,organisation_id,id)
  WHERE retention_expires_at IS NOT NULL AND tombstoned_at IS NULL AND NOT legal_hold;
