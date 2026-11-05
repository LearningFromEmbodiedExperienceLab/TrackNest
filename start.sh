#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

exec .venv/bin/python - <<'PY'
import os
import subprocess
import sys
import time

import requests
from app import BASE_DIR, STATE_DIR

if not (STATE_DIR / "manager.db").is_file():
    raise SystemExit("请先按 README 初始化数据库并创建管理员。")

pid_file = STATE_DIR / "gunicorn.pid"
if pid_file.exists():
    try:
        pid = int(pid_file.read_text().strip())
        if pid <= 1:
            raise ValueError
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    except ValueError:
        raise SystemExit(f"PID 文件内容无效，请检查 {pid_file}")
    else:
        raise SystemExit(f"PID {pid} 仍在运行，无需重复启动；请运行 ./status.sh 查看状态。")

result = subprocess.run([
    sys.executable, "-m", "gunicorn", "--workers", "1", "--threads", "4",
    "--timeout", "60", "--bind", "127.0.0.1:9001", "--chdir", str(BASE_DIR),
    "--daemon", "--pid", str(pid_file),
    "--access-logfile", str(STATE_DIR / "gunicorn-access.log"),
    "--error-logfile", str(STATE_DIR / "gunicorn-error.log"),
    "--capture-output", "app:app",
])
if result.returncode:
    raise SystemExit(result.returncode)

session = requests.Session()
session.trust_env = False
for _ in range(100):
    try:
        if pid_file.exists() and session.get("http://127.0.0.1:9001/login", headers={"Host": "localhost:9000"}, timeout=1).status_code == 200:
            print("管理服务已启动：http://localhost:9000/login")
            print("实验实例请在登录后按需启动；运行 ./status.sh 检查统一入口。")
            break
    except requests.RequestException:
        pass
    time.sleep(0.2)
else:
    raise SystemExit(f"管理服务未就绪，请检查 {STATE_DIR / 'gunicorn-error.log'}")
PY
