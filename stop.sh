#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

exec .venv/bin/python - "$@" <<'PY'
import os
from pathlib import Path
import signal
import socket
import time

from script_output import script_parser

parser, console = script_parser("./stop.sh", "Stop the management service and experiment instances", "停止管理服务和全部实验实例")
parser.parse_args()

from app import app, BASE_DIR, STATE_DIR, DATA_ROOT, instance_lock, SharedProject, TrackioUser, stop_trackio

console.say(f"\nManagement data directory: {STATE_DIR}", f"\n管理数据目录：{STATE_DIR}")
console.say(f"\nExperiment data directory: {DATA_ROOT}", f"\n实验数据目录：{DATA_ROOT}")


def manager_matches(pid):
    try:
        process = Path(f"/proc/{pid}")
        command = (process / "cmdline").read_bytes()
        environment = dict(item.split(b"=", 1) for item in (process / "environ").read_bytes().split(b"\0") if b"=" in item)
        state = environment.get(b"MANAGER_STATE_DIR", str(BASE_DIR / "instance").encode())
        if not Path(os.fsdecode(state)).is_absolute():
            state = os.fsencode(BASE_DIR / os.fsdecode(state))
        return (process / "cwd").resolve() == BASE_DIR and b"gunicorn" in command and b"app:app" in command and Path(os.fsdecode(state)).resolve() == STATE_DIR
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return False


pid_file = STATE_DIR / "gunicorn.pid"
if pid_file.exists():
    try:
        pid = int(pid_file.read_text().strip())
        if pid <= 1:
            raise ValueError
    except ValueError:
        console.fail(f"\nInvalid PID file; no processes stopped: {pid_file}", f"\nPID 文件内容无效，未停止任何进程：{pid_file}")
    if manager_matches(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(200):
            if not manager_matches(pid):
                break
            time.sleep(0.2)
        else:
            console.fail("\nManagement process did not exit within 40 seconds; check the logs and retry.", "\n管理进程未在 40 秒内退出，请检查日志后重试。")
        console.say("\nManagement service stopped.", "\n管理服务已停止。", "success")
    elif Path(f"/proc/{pid}/cmdline").exists() and Path(f"/proc/{pid}/cmdline").read_bytes():
        console.fail("\nPID does not belong to this application; refusing to stop it. Check the PID file.", "\nPID 不属于本应用，拒绝停止该进程；请检查 PID 文件。")
    pid_file.unlink(missing_ok=True)

# An older, manually started manager may not have a PID file.
with socket.socket() as sock:
    sock.settimeout(1)
    if sock.connect_ex(("127.0.0.1", 9001)) == 0:
        console.fail("\nPort 9001 is still in use; stop the manually started management process and retry.", "\n9001 端口仍被占用，请先停止此前手动启动的管理进程，再重试。")

if (STATE_DIR / "manager.db").exists():
    with app.app_context(), instance_lock():
        for backend in [*SharedProject.query.all(), *TrackioUser.query.all()]:
            stop_trackio(backend)
console.say("\nAll experiment instances stopped; data retained. Nginx remains running.", "\n全部实验实例已停止，数据保留。Nginx 保持运行。", "success")
PY
