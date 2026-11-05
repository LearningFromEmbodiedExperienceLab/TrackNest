#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

exec .venv/bin/python - "$@" <<'PY'
import requests
from storage_config import storage_paths
from script_output import script_parser

parser, console = script_parser("./status.sh", "Check management and experiment status", "检查管理服务和实验实例状态")
parser.parse_args()
state, data_root = storage_paths()
console.say(f"\nManagement data directory: {state}", f"\n管理数据目录：{state}")
console.say(f"\nExperiment data directory: {data_root}", f"\n实验数据目录：{data_root}")
session = requests.Session()
session.trust_env = False
healthy = True
for english, chinese, url in (
    ("Management backend", "管理后端", "http://127.0.0.1:9001/login"),
    ("Public entry point", "统一入口", "http://localhost:9000/login"),
):
    try:
        response = session.get(url, headers={"Host": "localhost:9000"}, timeout=3, allow_redirects=False)
        ok = response.status_code == 200
        console.say(f"\n{english}: HTTP {response.status_code} {'OK' if ok else 'ERROR'}",
                    f"\n{chinese}：HTTP {response.status_code} {'正常' if ok else '异常'}", "success" if ok else "error")
    except requests.RequestException:
        ok = False
        console.say(f"\n{english}: unable to connect", f"\n{chinese}：无法连接", "error")
    healthy = healthy and ok

pid_file = state / "gunicorn.pid"
pid = pid_file.read_text().strip() if pid_file.exists() else console.text("none (not managed by start.sh)", "无（未由 start.sh 管理）")
console.say(f"\nManagement PID: {pid}", f"\n管理 PID：{pid}")
console.say(f"\nManagement log: {state / 'gunicorn-error.log'}", f"\n管理日志：{state / 'gunicorn-error.log'}")
if not (state / "manager.db").exists():
    console.fail("\nDatabase is not initialized.", "\n数据库未初始化。")

from app import app, SharedProject, TrackioUser, is_running

with app.app_context():
    for label, chinese, backends in (
        ("User", "用户", TrackioUser.query.order_by(TrackioUser.username)),
        ("Project", "项目", SharedProject.query.order_by(SharedProject.id)),
    ):
        for backend in backends:
            running = is_running(backend)
            unexpected = bool(backend.pid and not running)
            name = backend.username if label == "User" else f"{backend.owner.username}/{backend.name}"
            english_status = "running" if running else "unexpectedly stopped" if unexpected else "stopped"
            chinese_status = "运行中" if running else "异常停止" if unexpected else "已停止"
            console.say(f"\n{label} {name}: {english_status}, port {backend.port}",
                        f"\n{chinese} {name}：{chinese_status}，端口 {backend.port}",
                        "success" if running else "error" if unexpected else "warning")
            if unexpected:
                healthy = False
raise SystemExit(0 if healthy else 1)
PY
