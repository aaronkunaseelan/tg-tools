# Configuration
Services read the process environment; systemd loads `~/.config/agent-secrets/runtime-controller.env` (protect it as 0600). Local examples use `.env.example` / `~/tools/tools.env`; source that file explicitly, never commit secrets.
## API/database
- `RUNTIME_CONTROLLER_API_URL`: client URL, e.g. `http://127.0.0.1:8787`; `RUNTIME_CONTROLLER_BIND_HOST`, `RUNTIME_CONTROLLER_PORT`: listener (default loopback, 8787).
- `RUNTIME_CONTROLLER_BEARER_TOKEN`: API auth; e.g. `replace-me` (never a production credential).
- `RUNTIME_CONTROLLER_DATABASE_URL` or `DATABASE_URL`: Postgres DSN; `RUNTIME_CONTROLLER_DATABASE_NAME` or `PGDATABASE`: database name.
- `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`: libpq connection settings; `USER` supplies local peer user. Example: private socket directory, port 5432, user `example`.
- `RUNTIME_CONTROLLER_APP_NAME`: Postgres application name; `RUNTIME_CONTROLLER_DB_MAX`: connection pool size (10).
- `RUNTIME_CONTROLLER_POLICY_FILE`: YAML admission/placement policy, e.g. `/path/to/runtime-policy.yaml`; schema and complete keys in `config/runtime-policy.example.yaml`.
- `RUNTIME_CONTROLLER_WORKER_HEARTBEAT_TTL_SEC`: active membership freshness window (120 seconds).
- `RUNTIME_CONTROLLER_VERSION`: health/version label (e.g. `dev`).
## Reconciler
- `RUNTIME_CONTROLLER_RECONCILE_INTERVAL_SEC`: sweep cadence (30); `RUNTIME_CONTROLLER_RECONCILE_LEASE_LIMIT`, `RUNTIME_CONTROLLER_RECONCILE_DEADLINE_LIMIT`, `RUNTIME_CONTROLLER_RECONCILE_DELIVERY_LIMIT`: rows per sweep (100 each).
- `RUNTIME_CONTROLLER_BASE_BACKOFF_SEC`, `RUNTIME_CONTROLLER_MAX_BACKOFF_SEC`: retry bounds (60, 3600).
## Worker
- `RUNTIME_CONTROLLER_WORKER_ID`: membership identity (e.g. `example-worker`), otherwise generated from `HOSTNAME` and PID.
- `RUNTIME_CONTROLLER_WORKER_VENUE`: `generic`, `oci` or `modal`; `RUNTIME_CONTROLLER_WORKER_CAPACITY`: concurrent claims (1).
- `RUNTIME_CONTROLLER_WORKER_SUPPORTED_PROFILES`, `RUNTIME_CONTROLLER_WORKER_SUPPORTED_WORKLOAD_TYPES`, `RUNTIME_CONTROLLER_WORKER_ALLOWED_PRODUCER_SYSTEMS`: comma-separated eligibility filters.
- `RUNTIME_CONTROLLER_WORKER_ALLOWED_QUEUE_CLASSES`, `RUNTIME_CONTROLLER_WORKER_QUEUE_PREFERENCE`: comma-separated queue filters/order, e.g. `foreground,background,ops`.
- `RUNTIME_CONTROLLER_WORKER_POLL_INTERVAL_MS`, `RUNTIME_CONTROLLER_WORKER_LEASE_DURATION_SEC`, `RUNTIME_CONTROLLER_WORKER_HEARTBEAT_INTERVAL_SEC`: polling, lease and heartbeat (2000, 120, 30).
## QMD bridge / Modal
- `RUNTIME_CONTROLLER_QMD_REPO_ROOT`: required checkout path at worker/bridge/Modal startup, e.g. `/path/to/qmd`; earlier builds defaulted to `~/tools/qmd`. Configure it explicitly before promotion.
- `RUNTIME_CONTROLLER_QMD_PYTHON`: interpreter (defaults to checkout `.venv/bin/python`).
- `RUNTIME_CONTROLLER_QMD_EMBED_BRIDGE_SCRIPT`: bridge entrypoint (bundled Python bridge by default).
- `RUNTIME_CONTROLLER_QMD_EMBED_ADAPTER`: `auto`, `reference_stub`, `qmd_bridge`; bridge payload `qmdRepoRoot` overrides the Python env root.
- Modal sets `QMD_EMBED_WORKER_VENUE=modal` if absent; QMD's own provider credentials/config remain QMD-owned.
## Tooling
`package.json` sets the Node/npm major versions (`engines`) and pins dependencies; exact host versions live in agent-tools-update's `runtimes.toml`. `deploy/no-secret-env.py` removes credentials from dependency subprocesses. See [release.md](release.md).
Tests require `RUNTIME_TEST_PGHOST` (absolute disposable socket directory), optional `RUNTIME_TEST_PGPORT` (5432), `RUNTIME_TEST_PGUSER` (ubuntu). Never point tests at the production socket.
