#!/usr/bin/env python3
"""Run dependency tooling without inherited account or registry credentials."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        raise SystemExit("usage: no-secret-env.py -- command [args...]")
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL")
           if key in os.environ}
    with tempfile.TemporaryDirectory(prefix="npm-no-secret-") as temp:
        # npm rejects loading the same file as both global and user config.
        user = Path(temp) / "user.npmrc"
        global_config = Path(temp) / "global.npmrc"
        user.touch()
        global_config.touch()
        env.update(HOME=temp, TMPDIR=temp, XDG_CONFIG_HOME=str(Path(temp) / "config"),
                   XDG_CACHE_HOME=str(Path(temp) / "cache"),
                   npm_config_userconfig=str(user), npm_config_globalconfig=str(global_config),
                   npm_config_registry="https://registry.npmjs.org/",
                   npm_config_ignore_scripts="true", npm_config_engine_strict="true")
        return subprocess.run(argv, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
