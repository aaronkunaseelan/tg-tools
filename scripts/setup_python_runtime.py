#!/usr/bin/env python3
"""Create a new hash-locked candidate runtime; never promote or reuse it."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
RUNTIMES = Path.home() / ".local/lib/tg-tools/runtimes"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", type=Path, required=True, help="Absolute path to uv 0.11.32")
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error("Python >=3.12 required")
    uv = args.uv
    if not uv.is_absolute() or not uv.is_file():
        parser.error("--uv must name an absolute executable path")
    # No inherited provider, browser, cloud, proxy, or index credentials.
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "UV_NO_CONFIG": "1",
           "UV_PYTHON_DOWNLOADS": "never"}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]
    if (ROOT / "requirements-workspace.txt").read_text() != "\n".join(dependencies) + "\n":
        parser.error("generated requirements disagree with pyproject")
    expected = dependencies + project["build-system"]["requires"] + project["tool"]["tg-tools"]["bootstrap-dependencies"]
    def pins(items: list[str]) -> dict[str, str]:
        result = {}
        for item in items:
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s<>=!~;]+)", item)
            if not match:
                parser.error("non-exact dependency pin")
            key = re.sub(r"[-_.]+", "-", match[1]).lower()
            if key in result:
                parser.error("duplicate dependency pin")
            result[key] = match[2]
        return result
    lock = ROOT / "requirements-workspace.lock"
    logical = "\n".join(line for line in lock.read_text().splitlines()
                        if line.strip() and not line.lstrip().startswith("#"))
    logical = logical.replace(chr(92) + "\n", " ")
    locked = []
    for line in logical.splitlines():
        tokens = line.split()
        if len(tokens) < 2 or any(not re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", t) for t in tokens[1:]):
            parser.error("lock must contain only exact pins with SHA256 hashes")
        locked.append(tokens[0])
    if pins(locked) != pins(expected):
        parser.error("hash lock disagrees with runtime/build/bootstrap pins")
    version = subprocess.check_output([str(uv), "--version"], text=True, env=env).strip()
    if version.split()[:2] != ["uv", "0.11.32"]:
        parser.error("expected uv 0.11.32")
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()
    identity = {"lock_sha256": lock_hash, "python": platform.python_version(),
                "system": platform.system(), "machine": platform.machine(),
                "uv": version, "base_python": str(Path(sys.executable).resolve()),
                "bootstrap_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    RUNTIMES.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = RUNTIMES / ("candidate-" + digest)
    # Reject even a previously successful target: no implicit reuse or repair.
    if target.exists() or target.is_symlink():
        parser.error("candidate target already exists; refusing to modify it")
    target.mkdir(mode=0o700)
    env["HOME"] = str(target)
    env["TMPDIR"] = str(target / "tmp")
    (target / "tmp").mkdir(mode=0o700)
    env["UV_CACHE_DIR"] = str(target / "uv-cache")
    def run(*command: str) -> None:
        subprocess.run(command, check=True, env=env, stdout=sys.stderr)
    run(str(uv), "venv", "--allow-existing", "--no-project", "--python", sys.executable, str(target))
    python = target / "bin/python"
    # Install only the locked build tools first, without fetching dependencies.
    blocks = re.split(r"(?m)(?=^[A-Za-z0-9_.-]+==)", lock.read_text())
    build_lock = target / "build-requirements.lock"
    build_lock.write_text("".join(b for b in blocks if b.startswith(("setuptools==", "wheel==", "packaging==", "pip=="))))
    run(str(uv), "pip", "install", "--python", str(python), "--require-hashes",
        "--no-deps", "--only-binary", ":all:", "-r", str(build_lock))
    # Build tooling is itself hash-locked. pyaes has only an sdist; disable
    # isolated builds so its build cannot fetch unpinned build dependencies.
    run(str(uv), "pip", "install", "--python", str(python), "--require-hashes",
        "--no-deps", "--no-build-isolation", "-r", str(lock))
    run(str(uv), "pip", "check", "--python", str(python))
    packages = json.loads(subprocess.check_output(
        [str(uv), "pip", "list", "--python", str(python), "--format", "json"], env=env, text=True))
    metadata = {**identity, "runtime": str(target), "packages": packages,
                "dependency_check": "passed", "production_modified": False}
    (target / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    main()
