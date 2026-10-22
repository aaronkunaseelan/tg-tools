# Node Runtime Policy

Scope: `~/tools/runtime-controller/`.

- Runtime: Node 24.20.0 (`.node-version`), supported policy range >=24 <25.
- Package manager: npm 12.0.2, pinned in packageManager/devEngines. Use
  `python3 deploy/no-secret-env.py -- npm ...` for dependency commands.
- Lock state: package-lock.json.
- Routine dependency commands must not inherit broad user, cloud, browser, Docker, SSH agent, or package-manager credentials.
- Prohibited routine commands: plain npm ci, routine npm install, unpinned npx/npm exec, broad npm update, npm audit fix --force, global npm latest upgrades, blind Docker pulls, and lockfile churn without a scoped change note.
- Lifecycle scripts remain disabled by default. Native/lifecycle execution requires an explicit allowlist and evidence for the specific package, ABI, artifact path, and runtime smoke.
- Keep .npmrc auth-free. Do not add registry tokens or secret-bearing config to this root.

Exact direct versions now match the existing lock; phase zero changes no transitive
versions and does not replace production node_modules. The package-owned wrapper
omits inherited account/cloud variables and disables user/global registry config.
The old maintenance-project wrapper and legacy workspace root are retired.
Qualify candidate dependencies separately, run typecheck/tests and record rollback
before any production promotion.
