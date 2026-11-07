"""Use ``import tracknest as trackio`` after ``python -m tracknest login``."""
import argparse
from collections import Counter
from contextlib import closing
from functools import wraps
import getpass
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlencode, urlsplit

import httpx2
import trackio as _sdk
from trackio.remote_client import RemoteClient as _RemoteClient
from trackio.sqlite_storage import SQLiteStorage

_server_url = None
_uploaded = Counter()
_last_report = {}


def _status(message, kind="info"):
    prefix = "tracknest: "
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        color = {"info": "36", "success": "32", "warning": "33", "error": "31"}[kind]
        prefix = "\033[1;34mtracknest\033[0m: "
        message = f"\033[{color}m{message}\033[0m"
    print(prefix + message, flush=True)


def _config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tracknest/client.json"


def _read_config():
    path = _config_path()
    if not path.exists():
        raise RuntimeError("Run tracknest login --server-url <personal-or-team-URL> first")
    return json.loads(path.read_text())


class _DirectClient(_RemoteClient):
    def __init__(self, space, *args, **kwargs):
        self._tracknest = space.rstrip("/") == (_server_url or "").rstrip("/")
        if self._tracknest:
            kwargs["httpx_kwargs"] = {**kwargs.get("httpx_kwargs", {}), "trust_env": False}
        super().__init__(space, *args, **kwargs)

    def predict(self, *args, api_name, **kwargs):
        try:
            result = super().predict(*args, api_name=api_name, **kwargs)
        except Exception as error:
            if self._tracknest and api_name.startswith("/bulk_"):
                _status(f"Upload failed; data will be buffered locally for retry: {error}", "error")
            raise
        if self._tracknest and api_name == "/bulk_log":
            for project, count in Counter(row["project"] for row in kwargs.get("logs", [])).items():
                _uploaded[project] += count
                now = time.monotonic()
                if now - _last_report.get(project, float("-inf")) >= 5:
                    _status(f"{project}: uploaded {_uploaded[project]} metric records in this process")
                    _last_report[project] = now
        return result


def _connect():
    global _server_url
    config = _read_config()
    _server_url = config["server_url"]
    _sdk.RemoteClient = _DirectClient
    importlib.import_module("trackio.run").RemoteClient = _DirectClient
    _status(f"Connecting to {_server_url} (only TrackNest uploads bypass proxies)")
    _DirectClient(_server_url, write_token=config["token"], verbose=False)
    _status("Connected. Token verified.", "success")
    return config


def init(*args, **kwargs):
    if kwargs.get("space_id") or os.environ.get("TRACKIO_SPACE_ID"):
        raise ValueError("TrackNest uses a self-hosted server; remove space_id / TRACKIO_SPACE_ID")
    config = _connect()
    if "server_url" in kwargs:
        raise ValueError("Configure the server with python -m tracknest login instead of passing server_url to init")
    kwargs["server_url"] = config["server_url"] + "?" + urlencode({"write_token": config["token"]})
    run = _sdk.init(*args, **kwargs)
    # Trackio 0.45 starts this flag as False even when an earlier process buffered data.
    with run._client_lock:
        run._has_local_buffer = SQLiteStorage.has_pending_data(run.project)
    original_finish = run.finish
    finished = False

    @wraps(original_finish)
    def finish_run():
        nonlocal finished
        if finished:
            return
        _status(f"{run.project}/{run.name}: waiting for uploads to finish...")
        original_finish()
        thread = run._client_thread
        pending = (thread is not None and thread.is_alive()) or SQLiteStorage.has_pending_data(run.project)
        with run._queue_lock:
            pending = pending or bool(run._sending_batch or run._queued_logs or run._queued_system_logs
                                      or run._queued_uploads or run._queued_alerts or run._has_local_buffer)
        if pending:
            _status(f"{run.project}: sync incomplete; pending data remains. Initialize the same project to retry.", "warning")
        else:
            _status(f"{run.project}: sync complete; uploaded {_uploaded[run.project]} metric records in this process", "success")
        finished = True

    run.finish = finish_run
    return run


def __getattr__(name):
    return getattr(_sdk, name)


def _login(args, parser):
    url = urlsplit(args.server_url)
    if url.scheme not in ("http", "https") or not url.netloc or url.query or url.fragment or url.username:
        parser.error("Use the http(s) personal/team URL without a token or query string")
    server_url = args.server_url.rstrip("/") + "/"
    token = getpass.getpass("TrackNest Token: ")
    if not token:
        parser.error("Token is required")
    _RemoteClient(server_url, write_token=token, httpx_kwargs={"trust_env": False}, verbose=False)
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        json.dump({"server_url": server_url, "token": token}, file)
    os.replace(file.name, path)
    _status(f"Connection verified. Settings saved to {path} (readable and writable only by your user).", "success")


def _pending_status(server_url):
    root = SQLiteStorage.get_project_db_path("_").parent
    _status(f"Local cache: {root}")
    found, readable = False, True
    for path in sorted(root.glob("*.db")):
        if path.name.startswith("registry-"):
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
                counts = []
                for table in ("metrics", "system_metrics", "traces", "pending_uploads"):
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    count = connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE rtrim(space_id, '/') = ?",
                        (server_url.rstrip("/"),),
                    ).fetchone()[0] if "space_id" in columns else 0
                    counts.append(count)
            if any(counts):
                found = True
                _status(f"{path.stem}: pending {counts[0]} metrics, {counts[1]} system records, "
                        f"{counts[2]} traces, {counts[3]} files", "warning")
        except (OSError, sqlite3.Error) as error:
            readable = False
            _status(f"Cannot inspect {path.name}: {error}", "warning")
    if not found and readable:
        _status("Local pending cache is empty for this server.", "success")
    _status("Cache counts exclude in-memory uploads in running training processes.")
    return readable


def main():
    parser = argparse.ArgumentParser(prog="tracknest", description="TrackNest training client")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Save and verify your server connection")
    login.add_argument("--server-url", required=True)
    commands.add_parser("status", help="Check connection and local pending data")
    commands.add_parser("projects", help="List remote projects")
    runs = commands.add_parser("runs", help="List runs in a remote project")
    runs.add_argument("project")
    args = parser.parse_args()
    try:
        if args.command == "login":
            _login(args, parser)
            return 0
        config = _read_config()
        if args.command == "status":
            _status(f"Config: {_config_path()}")
            _status(f"Server: {config['server_url']}")
            reachable = True
            try:
                _RemoteClient(config["server_url"], write_token=config["token"],
                              httpx_kwargs={"trust_env": False}, verbose=False)
                _status("Connection: connected; token verified.", "success")
            except (OSError, httpx2.HTTPError, ValueError, RuntimeError) as error:
                reachable = False
                _status(f"Connection: unavailable ({error})", "error")
            readable = _pending_status(config["server_url"])
            return 0 if reachable and readable else 1
        remote = _RemoteClient(config["server_url"], write_token=config["token"],
                               httpx_kwargs={"trust_env": False}, verbose=False)
        if args.command == "projects":
            projects = remote.predict(api_name="/get_all_projects")
            _status(f"Remote projects: {len(projects)}")
            for project in projects:
                print(f"  {project}")
        else:
            records = remote.predict(args.project, api_name="/get_runs_for_project")
            _status(f"Runs in {args.project}: {len(records)}")
            for record in records:
                print(f"  {record['name']}  id={record['id']}  created={record['created_at'] or '-'}")
        return 0
    except (OSError, httpx2.HTTPError, ValueError, RuntimeError) as error:
        _status(str(error), "error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
