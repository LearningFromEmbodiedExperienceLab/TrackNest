"""Use ``import tracknest as trackio`` after ``python -m tracknest login``."""
import argparse
from collections import Counter
from contextlib import closing
from functools import wraps
import getpass
import importlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unicodedata
from urllib.parse import urlencode, urljoin, urlsplit

_sdk = None
_server_url = None
_uploaded = Counter()
_last_report = {}
_language = "en"


def _text(english, chinese):
    return chinese if _language == "zh" else english


def _client_version():
    try:
        return version("tracknest-client")
    except PackageNotFoundError:
        return _text("source checkout", "源码版本")


def _display_width(value):
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in str(value))


def _table(headers, rows):
    numeric = {i for row in rows for i, value in enumerate(row) if isinstance(value, (int, float))}
    cells = [headers, *[tuple(str(value) for value in row) for row in rows]]
    widths = [max(_display_width(row[i]) for row in cells) for i in range(len(headers))]
    for index, row in enumerate(cells):
        columns = []
        for i, value in enumerate(row):
            padding = " " * (widths[i] - _display_width(value))
            cell = padding + value if i in numeric else value + padding
            if i == len(row) - 1:
                cell = cell.rstrip()
            columns.append(_color(cell, "heading" if index == 0 else "success" if i in numeric else "info"))
        print("  " + "  ".join(columns))
        if index == 0:
            _print("  " + "  ".join("-" * width for width in widths), "label")


def _error_message(error):
    cause = error
    while cause.__cause__ is not None and not isinstance(cause, httpx2.HTTPStatusError):
        cause = cause.__cause__
    if isinstance(cause, httpx2.HTTPStatusError):
        code = cause.response.status_code
        if code in (401, 403):
            return _text("Access denied. Check your token and instance permissions.", "访问被拒绝，请检查 Token 和实例操作权限。")
        if code == 404:
            return _text("TrackNest endpoint not found. Check the server URL and update the server if needed.", "找不到 TrackNest 接口，请检查服务器地址，必要时更新服务端。")
        if code == 503:
            return _text("Instance is stopped or unavailable. Run tracknest start, then retry.", "实例尚未启动或暂不可用，请执行 tracknest start 后重试。")
        return _text(f"Server request failed (HTTP {code}). Please retry later.", f"服务器请求失败（HTTP {code}），请稍后重试。")
    if isinstance(error, (httpx2.RequestError, ConnectionError)) or isinstance(cause, (httpx2.RequestError, ConnectionError)):
        return _text("Cannot reach TrackNest. Check the server address and SSH tunnel, then retry.", "无法连接 TrackNest，请检查服务器地址和 SSH 隧道后重试。")
    return str(error)


def _color(message, kind, file=None):
    if (file or sys.stdout).isatty() and "NO_COLOR" not in os.environ:
        color = {"info": "36", "success": "32", "warning": "33", "error": "31",
                 "heading": "1;35", "label": "34", "path": "35"}[kind]
        return f"\033[{color}m{message}\033[0m"
    return message


def _print(message, kind="info"):
    print(_color(message, kind))


def _status(message, kind="info"):
    prefix = _color("tracknest:", "label") + " "
    print(prefix + _color(message, kind), flush=True)


class _ArgumentParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        file = file or sys.stderr
        if message:
            file.write(_color(message, "error" if file is sys.stderr else "info", file=file))


def _config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tracknest/client.json"


def _read_config():
    path = _config_path()
    if not path.exists():
        raise RuntimeError(_text("Run tracknest login --server-url <personal-or-team-URL> first",
                                 "请先执行 tracknest login --server-url <个人或团队空间地址>"))
    return json.loads(path.read_text())


def _load_sdk():
    global _sdk, httpx2, _RemoteClient, SQLiteStorage, _DirectClient
    if _sdk is not None:
        return
    try:
        import trackio
        import httpx2
        from trackio.remote_client import RemoteClient as _RemoteClient
        from trackio.sqlite_storage import SQLiteStorage
    except ImportError as error:
        raise RuntimeError(_text(
            f"Trackio SDK is unavailable: {error}. TrackNest does not install dependencies automatically. "
            "Provide a compatible Trackio SDK separately (tested with 0.45.0), then retry.",
            f"Trackio SDK 不可用：{error}。TrackNest 不会自动安装依赖。"
            "请单独准备与现有环境兼容的 Trackio SDK（已验证 0.45.0），然后重试。")) from error

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

    _sdk = trackio


def _connect():
    global _server_url
    config = _read_config()
    _server_url = config["server_url"]
    _status(f"Connecting to {_server_url} (only TrackNest uploads bypass proxies)")
    state = _verify_credentials(_server_url, config["token"])
    if not state["instance_running"]:
        _status("Instance: stopped. Starting it before training...", "error")
        _manage_instance(config, "start")
        _status("Instance: running.", "success")
    _sdk.RemoteClient = _DirectClient
    importlib.import_module("trackio.run").RemoteClient = _DirectClient
    _DirectClient(_server_url, write_token=config["token"], verbose=False)
    _status("Connected. Token verified.", "success")
    return config


def init(*args, **kwargs):
    _load_sdk()
    if kwargs.get("space_id") or os.environ.get("TRACKIO_SPACE_ID"):
        raise ValueError("TrackNest uses a self-hosted server; remove space_id / TRACKIO_SPACE_ID")
    if "server_url" in kwargs:
        raise ValueError("Configure the server with python -m tracknest login instead of passing server_url to init")
    config = _connect()
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
    if name.startswith("__"):
        raise AttributeError(name)
    _load_sdk()
    return getattr(_sdk, name)


def _verify_credentials(server_url, token):
    response = httpx2.get(urljoin(server_url, "__tracknest/auth"),
                         headers={"X-Trackio-Write-Token": token}, trust_env=False, timeout=10)
    response.raise_for_status()
    state = response.json()
    if state.get("authenticated") is not True or not isinstance(state.get("instance_running"), bool):
        raise RuntimeError(_text("The server did not confirm TrackNest credentials", "服务器未确认 TrackNest 凭据，请检查服务器地址。"))
    return state


def _manage_instance(config, action):
    response = httpx2.post(urljoin(config["server_url"], "__tracknest/instance"),
                          headers={"X-Trackio-Write-Token": config["token"]},
                          json={"action": action}, trust_env=False, timeout=60)
    if response.status_code == 503:
        raise RuntimeError(_text(f"Instance failed to {action}. Check server.log on the server and try again.",
                                 f"实例{'启动' if action == 'start' else '停止'}失败，请检查服务端 server.log 后重试。"))
    response.raise_for_status()
    state = response.json()
    if state.get("instance_running") is not (action == "start"):
        raise RuntimeError(_text(f"The server did not confirm instance {action}", "服务器未确认实例操作结果，请执行 tracknest status 检查状态。"))
    return state


def _project_config(config, selector):
    response = httpx2.get(urljoin(config["server_url"], "/__tracknest/projects"),
                         headers={"X-Trackio-Write-Token": config["token"]}, trust_env=False, timeout=10)
    response.raise_for_status()
    projects = response.json()["projects"]
    matches = [project for project in projects if project["id"] == selector] or [project for project in projects if project["name"] == selector]
    if not matches:
        raise RuntimeError(_text(f"Team project '{selector}' not found or you do not have access. Check its name or ID in the web panel.",
                                 f"找不到团队项目“{selector}”或你没有访问权限，请在网页确认项目名称或 ID。"))
    if len(matches) > 1:
        choices = ", ".join(f"{project['owner']}: {project['id']}" for project in matches)
        raise RuntimeError(_text(f"Multiple team projects named '{selector}'. Use --project <ID>: {choices}",
                                 f"存在多个同名团队项目“{selector}”，请使用 --project <ID>：{choices}"))
    return {**config, "server_url": urljoin(config["server_url"], f"/p/{matches[0]['id']}/")}


def _login(args, parser):
    url = urlsplit(args.server_url)
    if url.scheme not in ("http", "https") or not url.netloc or url.query or url.fragment or url.username:
        parser.error(_text("Use the http(s) personal/team URL without a token or query string", "请使用不含 Token 或查询参数的 http(s) 个人或团队空间地址"))
    server_url = args.server_url.rstrip("/") + "/"
    token = getpass.getpass(_color(_text("TrackNest Token: ", "TrackNest 账号 Token："), "label", file=sys.stderr))
    if not token:
        parser.error(_text("Token is required", "Token 不能为空"))
    state = _verify_credentials(server_url, token)
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        json.dump({"server_url": server_url, "token": token}, file)
    os.replace(file.name, path)
    _status(_text(f"Login successful. Credentials verified. Settings saved to {path} (only your user can read/write).",
                  f"登录成功，凭据已验证。配置已保存到 {path}（仅当前用户可读写）。"), "success")
    if not state["instance_running"]:
        _status(_text("Instance: stopped. Run tracknest start or initialize training to start it.",
                      "实例：已停止。执行 tracknest start 或初始化训练即可启动。"), "error")


def _pending_status(server_url):
    root = SQLiteStorage.get_project_db_path("_").parent
    _print("\n" + _text("Pending uploads", "待上传缓存"), "heading")
    print("  " + _color(_text("Cache", "缓存目录") + ": ", "label") + _color(str(root), "path") + "\n")
    rows, readable = [], True
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
                rows.append((path.stem, *counts))
        except (OSError, sqlite3.Error) as error:
            readable = False
            _status(_text(f"Cannot inspect {path.name}: {error}", f"无法读取缓存 {path.name}：{error}"), "warning")
    if rows:
        totals = tuple(sum(row[i] for row in rows) for i in range(1, 5))
        _table((_text("Project", "项目"), _text("Metrics", "指标"), _text("System", "系统记录"),
                _text("Traces", "追踪"), _text("Files", "文件")),
               [*rows, (_text("Total", "合计"), *totals)])
        _print("\n  " + _text("Initialize the same project to retry pending uploads.", "初始化对应项目即可重试上传待传数据。"), "warning")
    elif readable:
        _print("  " + _text("No pending uploads for this server.", "当前服务器没有待上传数据。"), "success")
    _print("  " + _text("Counts exclude in-memory uploads in running training processes.", "统计不包含仍在训练进程内存中的上传队列。"))
    return readable


def main():
    print(flush=True)
    try:
        return _main()
    finally:
        print(flush=True)


def _main():
    global _language
    language_parser = _ArgumentParser(add_help=False)
    language_parser.add_argument("--lang", choices=("en", "zh"), default="en")
    language, _ = language_parser.parse_known_args()
    _language = language.lang
    parser = _ArgumentParser(prog="tracknest", description=_text("TrackNest training client", "TrackNest 训练客户端"))
    parser.add_argument("--version", action="version", version=f"%(prog)s {_client_version()}")
    parser.add_argument("--lang", choices=("en", "zh"), default=_language, help=_text("Output language (default: en)", "输出语言（默认：en）"))
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help=_text("Save and verify your server connection", "保存并验证服务器连接"))
    login.add_argument("--server-url", required=True)
    commands.add_parser("status", help=_text("Check connection and local pending data", "查看连接、实例状态和待上传缓存"))
    commands.add_parser("start", help=_text("Start the configured instance and wait until ready", "启动当前实例并等待就绪"))
    commands.add_parser("stop", help=_text("Stop the configured instance (affects all its training runs)", "停止当前实例，影响该实例的所有训练上报"))
    for name in ("status", "start", "stop"):
        commands.choices[name].add_argument("--project", metavar="NAME_OR_ID",
                                           help=_text("Target a team project by name or ID without changing login", "通过名称或 ID 选择团队项目实例，不修改登录配置"))
    commands.add_parser("projects", help=_text("List remote projects", "列出远端项目"))
    runs = commands.add_parser("runs", help=_text("List runs in a remote project", "查看指定项目的训练记录"))
    runs.add_argument("project", nargs="?", help=_text("Project name (required)", "项目名称（必填）"))
    for command in commands.choices.values():
        command.add_argument("--lang", choices=("en", "zh"), default=argparse.SUPPRESS, help=_text("Output language (default: en)", "输出语言（默认：en）"))
    args = parser.parse_args()
    if args.command == "runs" and args.project is None:
        _status(_text("Specify a project name to view its runs.", "请指定项目名称，以查看该项目的训练记录。"), "error")
        _print("\n  " + _text("Example:       tracknest runs my-project", "示例：    tracknest runs my-project --lang zh"))
        _print("  " + _text("List projects: tracknest projects", "查看项目：tracknest projects --lang zh"))
        return 2
    try:
        _load_sdk()
    except RuntimeError as error:
        _status(str(error), "error")
        return 1
    try:
        if args.command == "login":
            _login(args, parser)
            return 0
        config = _read_config()
        if args.command in ("status", "start", "stop") and args.project is not None:
            config = _project_config(config, args.project)
        if args.command in ("start", "stop"):
            _status(_text(f"Instance: requesting {args.command} at {config['server_url']}...",
                          f"正在{'启动' if args.command == 'start' else '停止'}实例：{config['server_url']} ..."))
            _manage_instance(config, args.command)
            _status(_text("Instance: running.", "实例：运行中。") if args.command == "start" else _text("Instance: stopped.", "实例：已停止。"),
                    "success" if args.command == "start" else "error")
            return 0
        if args.command == "status":
            _status(_text("Status", "状态"))
            state, error = None, None
            try:
                state = _verify_credentials(config["server_url"], config["token"])
            except (OSError, httpx2.HTTPError, ValueError, RuntimeError) as exc:
                error = _error_message(exc)
            instance = _text("unknown", "未知") if state is None else _text("running", "运行中") if state["instance_running"] else _text("stopped", "已停止")
            fields = [(_text("Server", "服务器"), config["server_url"], "path"),
                      (_text("Authentication", "鉴权"), _text("verified", "已验证") if state else _text("unavailable", "未能验证"), "success" if state else "error"),
                      (_text("Instance", "实例"), instance, "success" if state and state["instance_running"] else "error"),
                      (_text("Config", "配置文件"), str(_config_path()), "path")]
            width = max(_display_width(label) for label, _, _ in fields)
            print()
            for label, value, kind in fields:
                print("  " + _color(f"{label}:{' ' * (width - _display_width(label) + 2)}", "label") + _color(value, kind))
            if error:
                print()
                _status(error, "error")
            elif not state["instance_running"]:
                start = "tracknest start" + (f" --project {args.project}" if args.project is not None else "")
                _print("\n  " + _text(f"Start the instance: {start}", f"启动实例：{start} --lang zh"), "warning")
            readable = _pending_status(config["server_url"])
            return 0 if state and state["instance_running"] and readable else 1
        state = _verify_credentials(config["server_url"], config["token"])
        if not state["instance_running"]:
            _status(_text("Instance is stopped. Run tracknest start, then retry this command.",
                          "实例尚未启动，请执行 tracknest start --lang zh 后重试本命令。"), "error")
            return 1
        remote = _RemoteClient(config["server_url"], write_token=config["token"],
                               httpx_kwargs={"trust_env": False}, verbose=False)
        if args.command == "projects":
            projects = remote.predict(api_name="/get_all_projects")
            _status(_text(f"Remote projects: {len(projects)}", f"远端项目：{len(projects)}"))
            if projects:
                _table((_text("Project", "项目"),), [(project,) for project in projects])
        else:
            records = remote.predict(args.project, api_name="/get_runs_for_project")
            _status(_text(f"Runs in {args.project}: {len(records)}", f"项目 {args.project} 的训练记录：{len(records)}"))
            if records:
                _table((_text("Run", "训练记录"), "ID", _text("Created", "创建时间")),
                       [(record["name"], record["id"], record["created_at"] or "-") for record in records])
        return 0
    except (OSError, httpx2.HTTPError, ValueError, RuntimeError) as error:
        message = _error_message(error)
        _status(_text(f"Login failed: {message}", f"登录失败：{message}") if args.command == "login" else message, "error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
