"""Small environment validation shared by product settings."""

import os
from collections.abc import Mapping


def required_env(name: str, env: Mapping[str, str] | None = None) -> str:
    value = (os.environ if env is None else env).get(name, "")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value
