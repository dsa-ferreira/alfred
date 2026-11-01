"""``alfred`` command line: ``serve`` and ``migrate``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def cmd_serve() -> int:
    from alfred.config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"alfred: {exc}", file=sys.stderr)
        return 2

    import asyncio

    from alfred.app import configure_logging, serve

    configure_logging(config.server.log_level)
    try:
        asyncio.run(serve(config))
    except RuntimeError as exc:
        print(f"alfred: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_migrate() -> int:
    # Needs only ALFRED_DATABASE_URL, so migrations run without the rest of the config.
    from alfred.config import ConfigError, load_database_url
    from alfred.db import migrate

    try:
        url = load_database_url()
    except ConfigError as exc:
        print(f"alfred: {exc}", file=sys.stderr)
        return 2
    migrate.upgrade(url=url)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alfred")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the bot, the service API and the AI worker")
    sub.add_parser("migrate", help="apply database migrations (alembic upgrade head)")
    args = parser.parse_args(argv)
    if args.command == "serve":
        return cmd_serve()
    return cmd_migrate()


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    run()
