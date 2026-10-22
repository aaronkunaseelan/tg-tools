from __future__ import annotations

import argparse
import logging
import sys

from .config import load_settings
from .document_tools import startup_check
from .worker import main as worker_main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tts_worker")
    parser.add_argument(
        "--startup-check", action="store_true",
        help="validate local dependencies without registering a worker")
    args = parser.parse_args(argv)
    if not args.startup_check:
        worker_main()
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    fatal, warnings = startup_check(load_settings())
    for problem in warnings:
        logging.warning("startup validation: %s", problem)
    for problem in fatal:
        logging.error("startup validation: %s", problem)
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
