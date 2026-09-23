ALTER TABLE access_events DROP CONSTRAINT access_events_action_check;
ALTER TABLE access_events ADD CONSTRAINT access_events_action_check
  CHECK (action IN ('REVIEW_SAVED','AUDIO_ACCESS_GRANTED','FINDINGS_EXPORTED'));

ALTER TABLE access_events DROP CONSTRAINT access_events_resource_type_check;
ALTER TABLE access_events ADD CONSTRAINT access_events_resource_type_check
  CHECK (resource_type IN ('AUDIT_REVIEW','AUDIO_OBJECT','FINDINGS_EXPORT'));
