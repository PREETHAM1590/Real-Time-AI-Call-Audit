-- Aggregate, cross-process worker stage/outcome counters for the metrics exporter.
-- Contains no tenant, call, job or agent identifiers -- only bounded stage/outcome
-- enum labels and running totals, updated by an upsert per claimed job attempt.
CREATE TABLE worker_stage_counters (
  stage text NOT NULL,
  outcome text NOT NULL,
  attempts bigint NOT NULL DEFAULT 0,
  total_duration_ms bigint NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (stage, outcome)
);
