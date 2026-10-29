# Data-free health summary

Reader: tg-tools telemetry (then supervisor watch and mech-d).
`GET /v2/health-summary?workloadTypes=tts_synthesize,x_tts_explain&resultOwners=tts,x-tts&windowSec=21600`
uses the normal bearer boundary. Workload/owner names are ASCII protocol words,
1–80 characters, 1–16 distinct names each; window defaults to 6 hours and is capped
at 7 days. `node dist/src/cli.js contract runtime.health-summary/2` prints the
shipped `contracts/runtime.health-summary-2.json` contract.

Every key is present. No job/delivery ids, input, output, payload, error text or
worker metadata are returned. Error classes outside the protocol-word shape are
reported as `unclassified`, never echoed. Jobs becoming dead are counted by their
immutable terminal result creation time; deliveries use `dead_at`, not creation
or last update. Superseded deliveries (`product_delivery_superseded`) remain
terminal `dead` rows for dedupe/replay fencing but are not failures. Their window
count is `supersededDeliveries`; `failuresComplete` covers both that count and
`failures`, including superseded rows in the history cap. Other dead deliveries
(including missing/unclassified errors) remain failures. Delivery failures include their associated job's workload, or
result owner when the delivery has no job (standalone notifications).

Per workload: latest job creation, status counts, oldest **current attempt** age
(from `started_at`, never lease renewal or job creation), any past job deadline,
and any expired active lease. Jobs retain `status=leased` while their current
attempt is leased/running. All exact counts are computed over at most 10,001
indexed rows. Each count/history/active read has that cap; a cap hit sets its
`countsComplete`, `activeComplete` or `failuresComplete` false. Counts/absence
must not be treated as authoritative when incomplete. This bounds cost without
adding lifetime counters or scanning an ever-growing history; consumers must
show unknown and omit capped metrics. A 1–7 day window is also enforced on
terminal failures. Active jobs and latest creation are independent of the
history cap. At most 16 workloads and 16 owners are processed per request.
Migration `012_health_summary.sql` adds four supporting indexes only.

Version 2 changes the meaning of failures, so it bumps the contract rather than
silently changing version 1. `/v1/health-summary` and its shipped `/1` contract
retain their original all-dead-deliveries behavior for producer-first promotion.
Promote this controller first, then tg-tools' `/2` reader. No new migration is
needed. `/healthz` and delivery status/finalize/reconcile APIs are unchanged.

`/healthz` is deliberately unchanged: reference workers retry it during startup
at `RUNTIME_CONTROLLER_WORKER_POLL_INTERVAL_MS` (default 2000ms). Installed
reference-worker and reconciler units also have an ExecStartPre curl probe
(maximum 10s, once per start). RSS and meeting clients expose health readers;
CLI/operator and monitoring callers also use it. New history queries do not belong on that startup hot path.
The dedicated view is called once every five minutes by tg-tools.
