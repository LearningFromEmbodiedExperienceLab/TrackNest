#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

exec .venv/bin/python - <<'PY'
from pathlib import Path
import os

import requests

state = Path(os.environ.get("MANAGER_STATE_DIR", "instance")).resolve()
session = requests.Session()
session.trust_env = False
healthy = True
for label, url in (
    ("管理后端", "http://127.0.0.1:9001/login"),
    ("统一入口", "http://localhost:9000/login"),
):
    try:
        response = session.get(url, headers={"Host": "localhost:9000"}, timeout=3, allow_redirects=False)
        ok = response.status_code == 200
        print(f"{label}：HTTP {response.status_code} {'正常' if ok else '异常'}")
    except requests.RequestException:
        ok = False
        print(f"{label}：无法连接")
    healthy = healthy and ok

pid_file = state / "gunicorn.pid"
print(f"管理 PID：{pid_file.read_text().strip() if pid_file.exists() else '无（未由 start.sh 管理）'}")
print(f"管理日志：{state / 'gunicorn-error.log'}")
if not (state / "manager.db").exists():
    raise SystemExit("数据库未初始化。")

from app import app, SharedProject, TrackioUser, is_running

with app.app_context():
    for user in TrackioUser.query.order_by(TrackioUser.username):
        running = is_running(user)
        print(f"用户 {user.username}：{'运行中' if running else '已停止'}，端口 {user.port}")
        if user.pid and not running:
            healthy = False
    for project in SharedProject.query.order_by(SharedProject.id):
        running = is_running(project)
        print(f"项目 {project.owner.username}/{project.name}：{'运行中' if running else '已停止'}，端口 {project.port}")
        if project.pid and not running:
            healthy = False
raise SystemExit(0 if healthy else 1)
PY
