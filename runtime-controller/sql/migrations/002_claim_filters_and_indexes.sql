BEGIN;

CREATE INDEX IF NOT EXISTS jobs_claim_by_profile_workload_idx
  ON runtime.jobs (execution_profile, workload_type, queue_class, priority DESC, runnable_at ASC, job_id)
  WHERE status IN ('pending', 'retryable');

COMMIT;
