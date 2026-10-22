"""tg-tools CLI: validate and explain the Telegram service manifests."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

from tg_tools_common.telegram_manifest import SERVICES_DIR, load_named_service_manifest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tg-tools")
    commands = parser.add_subparsers(required=True)
    validate = commands.add_parser("validate").add_subparsers(required=True).add_parser("service")
    validate.add_argument("name")
    validate.set_defaults(func=lambda args: print(f"service {_manifest(args.name).service} valid"))
    explain = commands.add_parser("routes").add_subparsers(required=True).add_parser("explain")
    explain.add_argument("--service", required=True)
    explain.add_argument("--json", action="store_true", required=True)
    explain.set_defaults(func=lambda args: print(json.dumps(_jsonable(asdict(_manifest(args.service))),
                                                            sort_keys=True)))
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ValueError as exc:
        parser.exit(2, f"tg-tools: {exc}\n")
    return 0


def _manifest(name: str):
    known = sorted(path.stem for path in SERVICES_DIR.glob("*.json"))
    if name not in known:
        raise ValueError(f"unknown service {name!r}; known: {', '.join(known)}")
    return load_named_service_manifest(name)


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
