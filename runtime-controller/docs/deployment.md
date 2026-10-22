# Deployment

## Deployment shape

Current deployment consists of three long-running processes:

1. **HTTP API**
   - producer and worker control plane
   - submission, source receipt, delivery outbox, worker registry, claim,
     heartbeat, finalize, cancel, reconcile, and result endpoints
   - health endpoint at `/healthz`
2. **Reconciler**
   - lease-expiry recovery
   - deadline-expiry recovery
   - delivery-claim expiry recovery
   - periodic retry/dead-letter transitions
3. **Reference worker**
   - claims compatible jobs through the HTTP API
   - marks attempts running
   - heartbeats active leases
   - finalizes success or simulated retryable / terminal outcomes

The runtime still does **not** ship with domain-specific business workers. The reference worker is a deployment and protocol aid, not the final producer-specific execution layer.

## 1. Create the database

Example local Postgres setup:

```bash
createdb runtime_controller
```

If you are using a different database name or a connection string, set:

- `RUNTIME_CONTROLLER_DATABASE_NAME`
- or `RUNTIME_CONTROLLER_DATABASE_URL`

## 2. Build the repo

```bash
cd ~/tools/runtime-controller
npm install
npm run build
npm run typecheck
npm test
```

## 3. Apply migrations

```bash
cd ~/tools/runtime-controller
npm run migrate -- --database runtime_controller
```

## 4. Configure env

```bash
cp ~/tools/runtime-controller/.env.example ~/tools/tools.env
chmod 600 ~/tools/tools.env
$EDITOR ~/tools/tools.env
```

Minimum recommended changes:

- set the real database name or URL
- set a strong `RUNTIME_CONTROLLER_BEARER_TOKEN`
- optionally set `RUNTIME_CONTROLLER_POLICY_FILE`
- set `RUNTIME_CONTROLLER_WORKER_HEARTBEAT_TTL_SEC` if the 120-second default
  does not fit the worker heartbeat interval and expected scheduling delay

## 5. Start locally without systemd

### API

```bash
cd ~/tools/runtime-controller
set -a && source ~/tools/tools.env && set +a
npm run serve
```

### Reconciler

```bash
cd ~/tools/runtime-controller
set -a && source ~/tools/tools.env && set +a
npm run reconciler
```

### Reference worker

```bash
cd ~/tools/runtime-controller
set -a && source ~/tools/tools.env && set +a
npm run worker
```

If you want this worker to execute QMD embedding batches with the local `BAAI/bge-base-en-v1.5` FastEmbed path from `~/tools/qmd`, opt in explicitly in `tools.env`:

```bash
RUNTIME_CONTROLLER_WORKER_SUPPORTED_PROFILES=embed_batch
RUNTIME_CONTROLLER_WORKER_SUPPORTED_WORKLOAD_TYPES=qmd_embedding
RUNTIME_CONTROLLER_WORKER_ALLOWED_PRODUCER_SYSTEMS=qmd
RUNTIME_CONTROLLER_QMD_EMBED_ADAPTER=auto
RUNTIME_CONTROLLER_QMD_REPO_ROOT=/path/to/qmd
RUNTIME_CONTROLLER_QMD_PYTHON=/path/to/qmd/.venv/bin/python
```

`auto` uses the real QMD bridge when the repo + venv + bundled bridge script are present; otherwise it falls back to the reference stub.

## 6. Install systemd user units

See:

- `~/tools/runtime-controller/systemd/README.md`
- `~/tools/runtime-controller/deploy/README.md`

## 7. Health check

### CLI

```bash
cd ~/tools/runtime-controller
npm run cli -- health --database runtime_controller
```

### HTTP

```bash
curl http://127.0.0.1:8787/healthz
```

The health payload includes job counts, ready-result counts, delivery status
counts, source-receipt status counts, worker heartbeat age/freshness, and
per-runnable-queue eligible fresh capacity. `RUNTIME_CONTROLLER_WORKER_HEARTBEAT_TTL_SEC`
defaults to `120`. A stored `active` worker with an older heartbeat remains in
the database for lease fencing and audit, but is unavailable for queue-capacity
health. When runnable work has no fresh eligible worker, `/healthz` remains
HTTP 200 while the database/API is reachable and reports `runtime.degraded`, a
queue alert, and the oldest pending job identity/age. This lets worker startup
probes restore capacity instead of deadlocking on the alert. No restart rate is reported because the controller has no reliable restart
metadata source.

## 8. Example secured API call

```bash
export TOKEN='replace-me'

curl \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "producerSystem": "scorer",
    "producerRef": "item-1",
    "resultOwner": "owner-a",
    "resultRoute": "owner-a.route",
    "workloadType": "scorer",
    "queueClass": "foreground",
    "executionProfile": "cpu_small",
    "executionShape": "local_bounded",
    "idempotencyKey": "score:item-1"
  }' \
  http://127.0.0.1:8787/v1/submit
```

## Reverse proxy

On this OCI host, the live nginx deployment is suitable for a careful first exposure pattern:

- runtime app stays on `127.0.0.1:8787`
- nginx listens on port `80`
- nginx allows only localhost and Tailscale (`100.64.0.0/10`)
- `/v1/*` still requires `RUNTIME_CONTROLLER_BEARER_TOKEN`
- current live config path is `/etc/nginx/sites-available/runtime-controller`

If you later add a real public hostname and certificates, switch to the TLS-oriented template under `deploy/nginx/runtime-controller.conf`.

## Operational notes

- keep the API on localhost unless you have a reverse proxy or firewall in front of it
- if you expose it, require `RUNTIME_CONTROLLER_BEARER_TOKEN`
- restart the API after changing policy files or env vars
- run migrations before starting updated code
