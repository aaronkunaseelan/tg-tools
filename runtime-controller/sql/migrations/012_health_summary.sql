BEGIN;
-- Cover bounded latest/history reads, active-only reads and terminal-time windows.
CREATE INDEX jobs_health_latest_idx ON runtime.jobs (workload_type, created_at DESC) INCLUDE (status);
CREATE INDEX jobs_health_active_idx ON runtime.jobs (workload_type, job_id) INCLUDE (deadline_at) WHERE status = 'leased';
CREATE INDEX results_health_dead_idx ON runtime.job_results (created_at DESC) INCLUDE (job_id, error_class) WHERE result_kind = 'dead';
CREATE INDEX deliveries_health_dead_idx ON runtime.delivery_outbox (result_owner, dead_at DESC) INCLUDE (job_id, last_error_class) WHERE status = 'dead';
COMMIT;
