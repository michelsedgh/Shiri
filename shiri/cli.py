"""Explicit service entry points, read-only diagnostics, and opt-in migration."""
import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

from shiri.settings import Settings
from shiri.store import Store


def doctor(settings):
    report = {"platform": sys.platform, "python": sys.version.split()[0], "simulation": settings.simulation,
              "runtime_socket": str(settings.runtime_socket), "checks": []}
    for command in ["ip", "dhclient", "avahi-daemon", "dbus-daemon", "unshare", "gst-inspect-1.0",
                    "shairport-sync", "nqptp", "airptpd", "owntone"]:
        path = None
        if settings.binary_dir and command in {"shairport-sync", "nqptp", "airptpd", "owntone"}:
            for directory in [settings.binary_dir, settings.binary_dir / "bin", settings.binary_dir / "sbin"]:
                candidate = directory / command
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    path = str(candidate)
                    break
        else:
            path = shutil.which(command)
        report["checks"].append({"name": command, "available": bool(path), "path": path})
    for module in ["gi", "aiortc", "av"]:
        result = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, timeout=10)
        report["checks"].append({"name": "python:" + module, "available": result.returncode == 0})
    report["hardware_ready"] = sys.platform == "linux" and all(item["available"] for item in report["checks"])
    return report


def bootstrap(settings):
    settings.api_token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if not settings.api_token_file.exists():
        descriptor = os.open(settings.api_token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as token:
            token.write(secrets.token_urlsafe(48) + "\n")
            token.flush()
            os.fsync(token.fileno())
    with contextlib.closing(Store(settings.database, max_rooms=settings.max_rooms)):
        pass
    return {"database": str(settings.database), "token_file": str(settings.api_token_file)}


def main():
    parser = argparse.ArgumentParser(description="Shiri room audio control")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    actions = parser.add_subparsers(dest="command", required=True)
    serve = actions.add_parser("serve", help="Start the rootless API and browser UI")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--simulation", action="store_true", help="Use explicit fake devices on loopback")
    actions.add_parser("runtime", help="Start the privileged Linux audio broker")
    actions.add_parser("doctor", help="Report prerequisites without changing the system")
    actions.add_parser("bootstrap", help="Initialize storage and an installation admin token")
    tts_worker = actions.add_parser("tts-worker", help="Start the optional isolated local TTS model worker")
    tts_worker.add_argument("--host", default="127.0.0.1")
    tts_worker.add_argument("--port", type=int, default=8091)
    tts_worker.add_argument("--token-file", type=Path, required=True)
    tts_worker.add_argument("--cache-dir", type=Path)
    tts_worker.add_argument("--registry", type=Path)
    from shiri.tts.models import DEFAULT_MODEL_ID
    tts_worker.add_argument("--preload", default=DEFAULT_MODEL_ID)
    tts_worker.add_argument("--allow-download", action="store_true", help="Allow pinned model downloads during explicit loading")
    migration = actions.add_parser("migrate", help="Plan or atomically import the old JSON configuration")
    migration.add_argument("source", type=Path)
    migration.add_argument("--apply", action="store_true", help="Import as disabled rooms; source remains untouched")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    if args.command == "tts-worker":
        import uvicorn
        from shiri.tts.worker import create_worker_app, read_worker_token
        app = create_worker_app(token=read_worker_token(args.token_file), registry_file=args.registry,
                                cache_dir=args.cache_dir, allow_download=args.allow_download,
                                preload_model=args.preload or None)
        uvicorn.run(app, host=args.host, port=args.port, workers=1, timeout_graceful_shutdown=10,
                    proxy_headers=False)
    elif args.command == "serve":
        from dataclasses import replace
        import uvicorn
        from shiri.api import create_app
        settings = replace(settings, api_host=args.host or settings.api_host,
                           api_port=args.port or settings.api_port, simulation=args.simulation or settings.simulation)
        if settings.simulation and settings.api_host not in {"127.0.0.1", "::1", "localhost"}:
            parser.error("Simulation disables authentication and must bind to loopback")
        uvicorn.run(create_app(settings), host=settings.api_host, port=settings.api_port, workers=1,
                    timeout_graceful_shutdown=20, proxy_headers=True, forwarded_allow_ips=settings.trusted_proxy_ips)
    elif args.command == "runtime":
        from shiri.runtime.broker import run_broker
        asyncio.run(run_broker(settings))
    elif args.command == "migrate":
        with contextlib.closing(Store(settings.database, max_rooms=settings.max_rooms)) as store:
            print(store.import_legacy(args.source, dry_run=not args.apply).model_dump_json(indent=2))
    elif args.command == "bootstrap":
        print(json.dumps(bootstrap(settings), indent=2))
    elif args.command == "doctor":
        print(json.dumps(doctor(settings), indent=2))


if __name__ == "__main__":
    main()
