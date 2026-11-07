"""Use ``import tracknest as trackio`` after ``python -m tracknest login``."""
import argparse
from collections import Counter
from functools import wraps
import getpass
import importlib
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlencode, urlsplit

import trackio as _sdk
from trackio.remote_client import RemoteClient as _RemoteClient
from trackio.sqlite_storage import SQLiteStorage

_server_url = None
_uploaded = Counter()
_last_report = {}


def _config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tracknest/client.json"


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
                print(f"[TrackNest] 上传失败，数据将保留本地待重试：{error}", flush=True)
            raise
        if self._tracknest and api_name == "/bulk_log":
            for project, count in Counter(row["project"] for row in kwargs.get("logs", [])).items():
                _uploaded[project] += count
                now = time.monotonic()
                if now - _last_report.get(project, float("-inf")) >= 5:
                    print(f"[TrackNest] {project}：本次进程已上传 {_uploaded[project]} 条指标", flush=True)
                    _last_report[project] = now
        return result


def _connect():
    global _server_url
    path = _config_path()
    if not path.exists():
        raise RuntimeError("请先运行 python -m tracknest login --server-url <个人或团队空间URL>")
    config = json.loads(path.read_text())
    _server_url = config["server_url"]
    _sdk.RemoteClient = _DirectClient
    importlib.import_module("trackio.run").RemoteClient = _DirectClient
    print(f"[TrackNest] 正在连接 {_server_url}（仅上报连接绕过代理）", flush=True)
    _DirectClient(_server_url, write_token=config["token"], verbose=False)
    print("[TrackNest] 已连接，Token 验证通过", flush=True)
    return config


def init(*args, **kwargs):
    if kwargs.get("space_id") or os.environ.get("TRACKIO_SPACE_ID"):
        raise ValueError("TrackNest 使用自托管服务器，请移除 space_id / TRACKIO_SPACE_ID")
    config = _connect()
    if "server_url" in kwargs:
        raise ValueError("服务器地址由 python -m tracknest login 配置，无需向 init 传入 server_url")
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
        print(f"[TrackNest] {run.project}/{run.name}：正在等待上传完成…", flush=True)
        original_finish()
        thread = run._client_thread
        pending = (thread is not None and thread.is_alive()) or SQLiteStorage.has_pending_data(run.project)
        with run._queue_lock:
            pending = pending or bool(run._sending_batch or run._queued_logs or run._queued_system_logs
                                      or run._queued_uploads or run._queued_alerts or run._has_local_buffer)
        if pending:
            print(f"[TrackNest] {run.project}：同步未完成；本地仍有待传数据，下次初始化同一 project 时重试", flush=True)
        else:
            print(f"[TrackNest] {run.project}：同步完成，本次进程已上传 {_uploaded[run.project]} 条指标", flush=True)
        finished = True

    run.finish = finish_run
    return run


def __getattr__(name):
    return getattr(_sdk, name)


def main():
    parser = argparse.ArgumentParser(description="Configure TrackNest once for subsequent training runs")
    parser.add_argument("command", choices=["login"])
    parser.add_argument("--server-url", required=True)
    args = parser.parse_args()
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
    print(f"[TrackNest] 已验证并保存配置：{path}（仅当前用户可读写）")


if __name__ == "__main__":
    main()
