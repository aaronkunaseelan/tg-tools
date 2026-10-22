#!/usr/bin/env python3
"""Read-only, metadata-only inventory for RSS/scribe/TTS reconciliation.

Writes a private JSON ledger; never copies job input, result bodies, delivery
payloads, provider receipts, or error text. This does not authorize replay.
"""
import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SQL = """
SELECT json_build_object(
 'job_id', j.job_id, 'route', j.workload_type, 'status', j.status,
 'error_class', j.last_error_class, 'attempts', j.attempt_count,
 'max_attempts', j.max_attempts, 'created_at', j.created_at,
 'source_shape', CASE WHEN j.input_json ? 'localSourcePath' THEN 'staged_file'
                      WHEN j.input_json ? 'sourceUrl' THEN 'url'
                      WHEN j.input_json ? 'sourceText' THEN 'inline_text'
                      ELSE 'other' END,
 'result_id', r.result_id, 'result_kind', r.result_kind,
 'result_consumed', r.consumed_at IS NOT NULL,
 'deliveries', COALESCE((SELECT json_agg(json_build_object(
    'id', d.delivery_id, 'status', d.status, 'kind', d.delivery_kind,
    'attempts', d.attempt_count, 'error_class', d.last_error_class))
    FROM runtime.delivery_outbox d WHERE d.job_id=j.job_id), '[]'::json),
 'disposition', 'review_required'
)::text
FROM runtime.jobs j
LEFT JOIN runtime.job_results r ON r.result_id=j.terminal_result_id
WHERE j.status='dead' AND j.workload_type IN
 ('rss_publish','transcribe_audio','tts_synthesize','x_tts_explain')
ORDER BY j.workload_type,j.created_at,j.job_id;
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('refusing to overwrite existing ledger')
    rows = subprocess.check_output(['psql', '-X', '-d', 'runtime_controller', '-Atc', SQL], text=True)
    ledger = {'generated_at': datetime.now(timezone.utc).isoformat(),
              'policy': 'read-only snapshot; review each item before re-admission',
              'jobs': [json.loads(row) for row in rows.splitlines()]}
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(ledger, stream, indent=2)
        stream.write('\n')
    print(f"Wrote {len(ledger['jobs'])} metadata-only dead-job records to {args.output}")


if __name__ == '__main__':
    main()
