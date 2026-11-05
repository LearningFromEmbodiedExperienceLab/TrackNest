#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

exec .venv/bin/python - "$@" <<'PY'
from pathlib import Path
import socket
import subprocess
import sys
import time

from storage_config import select_storage, storage_paths
from script_output import script_parser

parser, console = script_parser("./start.sh", "Start the Trackio management service in the background", "后台启动 Trackio 多用户管理服务")
parser.add_argument("--storage-dir", type=Path, help=console.text("storage root containing instance/ and data/ (default: project directory)", "持久化根目录，内含 instance/ 和 data/；不传则使用项目默认目录"))
args = parser.parse_args()
with socket.socket() as sock:
    sock.settimeout(1)
    if sock.connect_ex(("127.0.0.1", 9001)) == 0:
        state, data = storage_paths()
        console.say(f"\nManagement data directory: {state}", f"\n管理数据目录：{state}")
        console.say(f"\nExperiment data directory: {data}", f"\n实验数据目录：{data}")
        console.fail("\nManagement port is in use; run ./stop.sh before starting or changing storage paths.", "\n管理端口已被占用；请先运行 ./stop.sh，再启动或切换存储路径。")
select_storage(args.storage_dir)

import requests
from app import app, BASE_DIR, STATE_DIR, DATA_ROOT, init_db
from data_transfer import relocate_data
from sqlalchemy.exc import SQLAlchemyError

console.say(f"\nManagement data directory: {STATE_DIR}", f"\n管理数据目录：{STATE_DIR}")
console.say(f"\nExperiment data directory: {DATA_ROOT}", f"\n实验数据目录：{DATA_ROOT}")

if not (STATE_DIR / "manager.db").is_file():
    console.fail("\nStorage paths saved. Initialize the database and create an administrator as described in README, then retry with the same options. For an existing deployment, copy instance/ and data/ first.", "\n当前路径已保存。请先按 README 初始化数据库并创建管理员，然后使用相同启动参数重试；已有部署请先拷贝 instance/ 和 data/。")

pid_file = STATE_DIR / "gunicorn.pid"
if pid_file.exists():
    try:
        pid = int(pid_file.read_text().strip())
        if pid <= 1:
            raise ValueError
    except ValueError:
        console.fail(f"\nInvalid PID file; check {pid_file}", f"\nPID 文件内容无效，请检查 {pid_file}")
    process = Path(f"/proc/{pid}")
    try:
        command = (process / "cmdline").read_bytes()
        running = (process / "cwd").resolve() == BASE_DIR and b"gunicorn" in command and b"app:app" in command
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        running = False
    if running:
        console.fail(f"\nPID {pid} is already running; use ./status.sh to check its status.", f"\nPID {pid} 仍在运行，无需重复启动；请运行 ./status.sh 查看状态。")
    pid_file.unlink()
    console.say("\nRemoved the stale management PID record.", "\n已清理旧部署遗留的管理 PID 记录。", "warning")

with app.app_context():
    try:
        init_db()
        if relocate_data():
            console.say("\nUpdated storage paths for the migrated deployment.", "\n已自动适配迁移后的数据目录。", "success")
    except (OSError, ValueError, RuntimeError, SQLAlchemyError) as exc:
        console.fail(f"\nFailed to adapt storage paths: {exc}", f"\n数据目录适配失败：{exc}")

result = subprocess.run([
    sys.executable, "-m", "gunicorn", "--workers", "1", "--threads", "4",
    "--timeout", "60", "--bind", "127.0.0.1:9001", "--chdir", str(BASE_DIR),
    "--daemon", "--pid", str(pid_file),
    "--access-logfile", str(STATE_DIR / "gunicorn-access.log"),
    "--error-logfile", str(STATE_DIR / "gunicorn-error.log"),
    "--capture-output", "app:app",
])
if result.returncode:
    console.say(f"\nGunicorn failed to start; check {STATE_DIR / 'gunicorn-error.log'}", f"\nGunicorn 启动失败，请检查 {STATE_DIR / 'gunicorn-error.log'}", "error")
    raise SystemExit(result.returncode)

session = requests.Session()
session.trust_env = False
for _ in range(100):
    try:
        if pid_file.exists() and session.get("http://127.0.0.1:9001/login", headers={"Host": "localhost:9000"}, timeout=1).status_code == 200:
            console.say("\nManagement service started: http://localhost:9000/login", "\n管理服务已启动：http://localhost:9000/login", "success")
            console.say("\nStart experiment instances after logging in; run ./status.sh to check the public entry point.", "\n实验实例请在登录后按需启动；运行 ./status.sh 检查统一入口。")
            break
    except requests.RequestException:
        pass
    time.sleep(0.2)
else:
    console.fail(f"\nManagement service is not ready; check {STATE_DIR / 'gunicorn-error.log'}", f"\n管理服务未就绪，请检查 {STATE_DIR / 'gunicorn-error.log'}")
PY
