CREATE OR REPLACE FUNCTION reject_exotel_integration_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Exotel integration history is immutable';
END;
$$;

DROP TRIGGER IF EXISTS exotel_integration_events_immutable ON exotel_integration_events;
CREATE TRIGGER exotel_integration_events_immutable BEFORE UPDATE OR DELETE ON exotel_integration_events
  FOR EACH ROW EXECUTE FUNCTION reject_exotel_integration_event_mutation();
