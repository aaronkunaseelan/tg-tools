"""Resolve provider API keys from a configured helper command.

Both speech synthesis and title generation can fetch their key by running a
command instead of holding it in the environment (the deployed default shells
out to ``az cognitiveservices account keys list``).

The command is split with ``shlex`` and executed directly, never through a
shell: a shell would turn an environment variable into an arbitrary
command-execution surface and mangle arguments with spaces or quotes, and
nothing about these plain argv invocations needs one.
"""

from __future__ import annotations

import shlex
import subprocess


class KeyCommandError(RuntimeError):
    """The configured key command could not produce a key."""


def resolve_key_command(key_cmd: str, *, timeout: int = 60) -> str:
    """Run ``key_cmd`` without a shell and return its trimmed stdout."""
    try:
        argv = shlex.split(key_cmd)
    except ValueError as exc:
        raise KeyCommandError(f"key command is not parseable: {exc}") from exc
    if not argv:
        raise KeyCommandError("key command is empty")
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeyCommandError(f"key command failed: {exc}") from exc
    key = result.stdout.strip()
    if not key:
        raise KeyCommandError("key command returned empty output")
    return key
