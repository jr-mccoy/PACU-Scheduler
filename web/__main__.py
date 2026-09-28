"""Entry point for ``python -m web`` — serves the web interface.

Listens on localhost only by default: reach it from other devices through
Tailscale (``tailscale serve``), not by exposing it to the network, since
the app has no login of its own.
"""

from __future__ import annotations

import argparse
import logging
import os

from scheduler.logging_config import configure_logging

DEFAULT_PORT = 8080

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m web", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=os.environ.get("PACU_DB", "nurse_schedule.db"),
        help="database file, shared with the desktop app (default: nurse_schedule.db, or $PACU_DB)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to listen on (default: 127.0.0.1, this machine only)",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT})"
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> None:
    configure_logging()
    args = parse_args(argv)

    from waitress import serve

    from .app import create_app

    app = create_app(os.path.abspath(args.db))
    logger.info("Serving %s on http://%s:%d", os.path.abspath(args.db), args.host, args.port)
    serve(app, host=args.host, port=args.port, threads=4)


if __name__ == "__main__":
    run()
