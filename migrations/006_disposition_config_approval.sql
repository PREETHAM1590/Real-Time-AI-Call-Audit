ALTER TABLE disposition_config_events
  DROP CONSTRAINT IF EXISTS disposition_config_events_action_check;

ALTER TABLE disposition_config_events
  ADD CONSTRAINT disposition_config_events_action_check
  CHECK (action IN ('STAGED','APPROVED','ACTIVATED','ROLLBACK','REPLAYED'));
