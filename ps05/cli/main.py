"""PS-05 command-line interface.

Subcommands::

    ps05 serve     # run the API (uvicorn ps05.api.main:app)
    ps05 worker    # run a Celery worker
    ps05 migrate   # apply Alembic migrations
    ps05 version   # print the package version
"""

import argparse
import subprocess
import sys


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the FastAPI app via uvicorn."""
    from ps05.settings import get_settings

    settings = get_settings()
    host = args.host or settings.host
    port = args.port if args.port is not None else settings.port
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "ps05.api.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    return subprocess.call(cmd)


def _cmd_worker(args: argparse.Namespace) -> int:
    """Run a Celery worker: ``celery -A ps05.workers.celery_app worker``."""
    from ps05.settings import get_settings

    settings = get_settings()
    concurrency = args.concurrency or settings.celery_concurrency
    log_level = "INFO"
    cmd = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "ps05.workers.celery_app",
        "worker",
        "--loglevel",
        log_level,
        "--concurrency",
        str(concurrency),
    ]
    return subprocess.call(cmd)


def _cmd_migrate(_args: argparse.Namespace) -> int:
    """Apply all Alembic migrations up to head (in-process)."""
    import alembic
    from alembic.config import Config

    from ps05.settings import get_settings

    settings = get_settings()
    config = Config()
    config.set_main_option("script_location", "alembic")
    if settings.database_url:
        config.set_main_option("sqlalchemy.url", settings.database_url)
    alembic.command.upgrade(config, "head")
    return 0


def _cmd_version(_args: argparse.Namespace) -> int:
    from ps05 import __version__

    print(f"ps05 {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ps05", description="PS-05 forensics platform CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the FastAPI app via uvicorn")
    p_serve.add_argument("--host", default=None, help="bind host (default: settings.host)")
    p_serve.add_argument("--port", type=int, default=None, help="bind port (default: settings.port)")
    p_serve.set_defaults(func=_cmd_serve)

    p_worker = sub.add_parser("worker", help="run a Celery worker")
    p_worker.add_argument("--concurrency", type=int, default=None, help="worker concurrency")
    p_worker.set_defaults(func=_cmd_worker)

    p_migrate = sub.add_parser("migrate", help="apply Alembic migrations to head")
    p_migrate.set_defaults(func=_cmd_migrate)

    p_version = sub.add_parser("version", help="print the package version")
    p_version.set_defaults(func=_cmd_version)

    return parser


def cli_entrypoint(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def main() -> None:
    """Console-script entry: run and exit with the dispatch code."""
    sys.exit(cli_entrypoint())


if __name__ == "__main__":
    main()
