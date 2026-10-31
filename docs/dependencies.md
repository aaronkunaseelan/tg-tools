# Python dependencies and runtimes

`pyproject.toml` is the exact-pin authority: service dependencies in
`project.dependencies`, build tools in `build-system.requires`, and the
validator's pip in `tool.tg-tools.bootstrap-dependencies`.
`requirements-workspace.txt` is generated from it, and
`requirements-workspace.lock` adds hashes for the full closure.

## Regenerate the lock

From the source root, with Python 3.12 and uv 0.11.32. The host's own uv moves on (pipx upgrades it), so the pinned one lives at
`~/.local/lib/tg-tools/uv-0.11.32/bin/uv` (a venv with `pip install uv==0.11.32`); use that path wherever `~/.local/bin/uv` appears below.

```sh
python3 scripts/generate_requirements_workspace.py
python3 scripts/generate_requirements_workspace.py --check
# In a local lock-input.txt (do not commit it), list the generated requirements, build-system.requires
# and tool.tg-tools.bootstrap-dependencies, in that order, then:
~/.local/bin/uv pip compile lock-input.txt \
  --generate-hashes --python-version 3.12 --no-header \
  --output-file requirements-workspace.lock
```

The closure consists only of declared exact pins. If a dependency adds a
transitive, declare its exact pin before regenerating; the runtime build fails
closed otherwise.

## Build a runtime

```sh
python3 scripts/setup_python_runtime.py --uv ~/.local/bin/uv
```

This checks the generated requirements and every lock pin against
`pyproject.toml` (no duplicates, unhashed or extra entries), then builds
`~/.local/lib/tg-tools/runtimes/candidate-<sha256>`. The identity covers the
lock and bootstrap bytes, Python version and interpreter, OS/architecture and
uv version. An existing target is never reused or repaired; a failed build
stays for inspection.

Installers see only PATH, LANG, uv no-config/no-download settings and
candidate-local HOME/TMPDIR/cache paths: no provider, cloud, proxy or index
credentials. Every install requires hashes and disables dependency discovery.
`pyaes` is source-only and builds with the locked setuptools, wheel and
packaging. `uv pip check` gates success; stdout is JSON package metadata only.
The lock is qualified on Ubuntu aarch64 / CPython 3.12.

## Switch runtimes

Validate the checkout with the candidate (`TG_TOOLS_PYTHON=<candidate>/bin/python
python3 scripts/validate_tg_tools.py --full`), point
`~/.local/lib/tg-tools/runtime` at it, and restart the units. Passing local
tests does not qualify live Telegram, database, provider or load behaviour;
run a canary per product after switching.
