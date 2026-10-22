# Immutable releases
Release IDs are full Git commit SHAs from now on; older directory IDs have unknown provenance.
Never run services from a checkout or edit an existing release.
Use the Node version in `.node-version` and pinned npm; see `docs/deployment.md` for configuration.

## Build and qualify (disposable worktree)
```sh
sha=$(git rev-parse <commit>)
git worktree add --detach /tmp/runtime-controller-build "$sha"
cd /tmp/runtime-controller-build
test -z "$(git status --porcelain)"
npm run deps:ci                         # credential-free npm ci --ignore-scripts
npm run build
npm run typecheck
# Bind these to a disposable PostgreSQL cluster, never the production socket:
export RUNTIME_TEST_PGHOST=/path/to/test-socket RUNTIME_TEST_PGPORT=55432
nice -n 10 npm test
```

## Materialize and migrate
Only proceed after every gate exits zero. Do not overlap a worker's running job or another promotion.
```sh
base="$HOME/.local/lib/runtime-controller"
release="$base/releases/$sha"
mkdir "$release"                        # must not already exist
cp -a dist package.json package-lock.json sql config deploy systemd python "$release/"
(cd "$release" && python3 deploy/no-secret-env.py -- npm ci --omit=dev --ignore-scripts)
# Load the protected deployment environment without displaying its values.
set -a; . "$HOME/.config/agent-secrets/runtime-controller.env"; set +a
export DATABASE_URL="${RUNTIME_CONTROLLER_DATABASE_URL:-${DATABASE_URL:-}}"
export PGDATABASE="${RUNTIME_CONTROLLER_DATABASE_NAME:-${PGDATABASE:-runtime_controller}}"
(cd "$release" && npm run migrate)      # CLI uses DATABASE_URL / PGDATABASE, not RUNTIME_CONTROLLER_*
previous=$(readlink -f "$base/current") # record this rollback target
ln -sfn "$release" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
systemctl --user restart runtime-controller-api.service
systemctl --user restart runtime-controller-reconciler.service
systemctl --user restart runtime-controller-worker.service
```
Units must use `current` for WorkingDirectory/ExecStart and a matching pinned Node.
Inspect API pre-start health probes before the maintenance window: they may require the old API reachable.

## Verify and rollback
Verify `readlink -f "$base/current"`, all three units active, `curl --fail http://127.0.0.1:8787/healthz`,
and `(cd "$base/current" && npm run cli -- health)` with the protected environment loaded.
Record SHA, gates, migrations, checks and previous target in the deployment ledger.
Rollback: `ln -sfn "$previous" "$base/current.next"; mv -Tf "$base/current.next" "$base/current"`,
then restart api → reconciler → worker and repeat verification.
A symlink rollback does not reverse migrations: review backward compatibility and back up before migrating.
