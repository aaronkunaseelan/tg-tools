# systemd

User service units for the standalone runtime-controller deployment.

## Expected checkout

- repo: `%h/tools/runtime-controller`
- env file: `%h/tools/tools.env`

## Install

```bash
mkdir -p ~/.config/systemd/user
cp ~/tools/runtime-controller/systemd/runtime-controller-api.service ~/.config/systemd/user/
cp ~/tools/runtime-controller/systemd/runtime-controller-reconciler.service ~/.config/systemd/user/
cp ~/tools/runtime-controller/systemd/runtime-controller-worker.service ~/.config/systemd/user/
cp ~/tools/runtime-controller/.env.example ~/tools/tools.env
chmod 600 ~/tools/tools.env
systemctl --user daemon-reload
systemctl --user enable --now runtime-controller-api.service runtime-controller-reconciler.service runtime-controller-worker.service
```

## Check

```bash
systemctl --user status runtime-controller-api.service runtime-controller-reconciler.service runtime-controller-worker.service
journalctl --user -u runtime-controller-api.service -n 100 --no-pager
journalctl --user -u runtime-controller-reconciler.service -n 100 --no-pager
journalctl --user -u runtime-controller-worker.service -n 100 --no-pager
curl http://127.0.0.1:8787/healthz
```

## Notes

- set `RUNTIME_CONTROLLER_BEARER_TOKEN` in the env file before exposing the API beyond localhost
- build the repo before starting services: `cd ~/tools/runtime-controller && npm run build`
- apply migrations before enabling the services
- `runtime-controller-worker.service` is a reference worker; in mixed-workload environments either disable it or constrain it with `RUNTIME_CONTROLLER_WORKER_SUPPORTED_WORKLOAD_TYPES`, `RUNTIME_CONTROLLER_WORKER_ALLOWED_QUEUE_CLASSES`, and optional `RUNTIME_CONTROLLER_WORKER_QUEUE_PREFERENCE` so it does not compete with dedicated workload workers
- if a worker should identify its venue for observability, set `RUNTIME_CONTROLLER_WORKER_VENUE=oci` or `RUNTIME_CONTROLLER_WORKER_VENUE=modal`; the default is `generic`
- to let `runtime-controller-worker.service` execute real `qmd_embedding` batches with the local `BAAI/bge-base-en-v1.5` model from `~/tools/qmd`, opt in with env like:
  - `RUNTIME_CONTROLLER_WORKER_SUPPORTED_PROFILES=embed_batch`
  - `RUNTIME_CONTROLLER_WORKER_SUPPORTED_WORKLOAD_TYPES=qmd_embedding`
  - `RUNTIME_CONTROLLER_WORKER_ALLOWED_PRODUCER_SYSTEMS=qmd`
  - `RUNTIME_CONTROLLER_QMD_EMBED_ADAPTER=auto`
  - `RUNTIME_CONTROLLER_QMD_REPO_ROOT=/path/to/qmd`
  - `RUNTIME_CONTROLLER_QMD_PYTHON=/path/to/qmd/.venv/bin/python`
