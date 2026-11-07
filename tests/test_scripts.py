"""Exercise lifecycle scripts on temporary data and unused ports."""
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import BASE_DIR


def test_script_lifecycle_and_pid_safety(tmp_path):
    root = tmp_path / "application with spaces"
    root.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        manager_port = sock.getsockname()[1]

    class Gateway(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    public_port = gateway.server_port
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    for name in ("app.py", "trackio_server.py", "data_transfer.py", "storage_config.py", "script_output.py", "personal_projects.py", "client_distribution.py"):
        shutil.copy(BASE_DIR / name, root / name)
    for name in (".venv", "templates", "static", "client"):
        (root / name).symlink_to(BASE_DIR / name, target_is_directory=True)
    for name in ("start.sh", "stop.sh", "status.sh"):
        script = (BASE_DIR / name).read_text()
        # Use disposable ports instead of touching the deployed application.
        script = script.replace("9001", str(manager_port)).replace("9000", str(public_port))
        (root / name).write_text(script)
        (root / name).chmod(0o755)
    env = {
        **os.environ,
        "MANAGER_STATE_DIR": str(root / "state"),
        "MANAGER_DATA_ROOT": str(root / "experiments"),
    }

    def python(code):
        return subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True, timeout=80)

    def run(name, deployment=root, config=env, args=()):
        # The caller need not be in the application directory.
        return subprocess.run([str(deployment / name), *args], cwd=tmp_path, env=config, capture_output=True, text=True, timeout=60)

    unrelated = None
    manager_pid = None
    relocated = None
    relocated_env = None
    try:
        assert run("start.sh", args=("--help",)).returncode == 0
        assert not (root / ".storage.json").exists()
        initial = run("start.sh", args=("--lang", "zh"))
        assert initial.returncode != 0 and "请先按 README 初始化数据库" in initial.stderr
        for script in ("start.sh", "stop.sh", "status.sh"):
            help_result = run(script, args=("--lang", "zh", "--help"))
            assert help_result.returncode == 0 and "输出语言" in help_result.stdout
        result = python("from app import app, init_db;\nwith app.app_context(): init_db()")
        assert result.returncode == 0, result.stderr
        assert run("status.sh").returncode == 1
        result = run("start.sh")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Management service started:" in result.stdout
        assert "\x1b[" not in result.stdout + result.stderr
        pid_file = root / "state/gunicorn.pid"
        manager_pid = int(pid_file.read_text())
        assert run("status.sh").returncode == 0
        assert run("start.sh").returncode != 0
        assert int(pid_file.read_text()) == manager_pid

        result = python('''
import secrets
from pathlib import Path
from app import app, db, create_user, SharedProject, get_free_port, start_trackio
with app.app_context():
    user = create_user("alice", "test-password-123")
    project = SharedProject(id="lifecycle-test", name="test-project", owner=user,
        port=get_free_port(), write_token=secrets.token_hex(32),
        data_dir=str(Path(user.data_dir) / "projects/lifecycle-test"))
    db.session.add(project)
    db.session.commit()
    for backend in (user, project):
        start_trackio(backend)
        (Path(backend.data_dir) / "retained.txt").write_text("experiment data")
''')
        assert result.returncode == 0, result.stdout + result.stderr
        status = run("status.sh")
        assert status.returncode == 0, status.stdout + status.stderr
        assert "User alice: running" in status.stdout
        assert "Project alice/test-project: running" in status.stdout

        with sqlite3.connect(root / "state/manager.db") as conn:
            user_pid = conn.execute("SELECT pid FROM trackio_user WHERE username='alice'").fetchone()[0]
            conn.execute("UPDATE trackio_user SET pid=1 WHERE username='alice'")
        try:
            assert run("status.sh").returncode == 1  # A recorded process disappeared.
        finally:
            with sqlite3.connect(root / "state/manager.db") as conn:
                conn.execute("UPDATE trackio_user SET pid=? WHERE username='alice'", (user_pid,))

        pid_file.unlink()
        refused = run("stop.sh")
        assert refused.returncode != 0 and "is still in use" in refused.stdout + refused.stderr
        pid_file.write_text(str(manager_pid))

        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        pid_file.write_text(str(unrelated.pid))
        refused = run("stop.sh")
        assert refused.returncode != 0 and "refusing to stop" in refused.stdout + refused.stderr
        assert unrelated.poll() is None
        pid_file.write_text(str(manager_pid))

        stopped = run("stop.sh", args=("--lang", "zh"))
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert "全部实验实例已停止" in stopped.stdout
        assert not pid_file.exists()
        assert run("stop.sh").returncode == 0
        assert run("status.sh").returncode == 1
        retained = list((root / "experiments").rglob("retained.txt"))
        assert len(retained) == 2 and all(path.read_text() == "experiment data" for path in retained)
        assert run("start.sh").returncode == 0
        manager_pid = int(pid_file.read_text())
        status = run("status.sh", args=("--lang", "zh"))
        assert status.returncode == 0
        assert "用户 alice：已停止" in status.stdout
        assert "项目 alice/test-project：已停止" in status.stdout
        assert run("stop.sh").returncode == 0
        relocated = tmp_path / "copied deployment"
        shutil.copytree(root, relocated, symlinks=True)
        relocated_env = {
            **env, "MANAGER_STATE_DIR": str(relocated / "state"),
            "MANAGER_DATA_ROOT": str(relocated / "experiments"),
        }
        (relocated / "state/gunicorn.pid").write_text(str(unrelated.pid))
        result = run("start.sh", relocated, relocated_env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert unrelated.poll() is None
        assert run("status.sh", relocated, relocated_env).returncode == 0
        assert (root / "state/secret.key").read_bytes() == (relocated / "state/secret.key").read_bytes()
        with sqlite3.connect(relocated / "state/manager.db") as conn:
            directories = conn.execute("SELECT data_dir FROM trackio_user UNION ALL SELECT data_dir FROM shared_project").fetchall()
        assert all(Path(row[0]).is_relative_to(relocated / "experiments") for row in directories)
        assert len(list((relocated / "experiments").rglob("retained.txt"))) == 2
        saved = (relocated / ".storage.json").read_bytes()
        assert run("start.sh", relocated, relocated_env, ("--storage-dir", str(tmp_path / "unused"))).returncode != 0
        assert (relocated / ".storage.json").read_bytes() == saved
        assert run("stop.sh", relocated, relocated_env).returncode == 0
        selected = tmp_path / "selected storage"
        selected.mkdir()
        shutil.copytree(relocated / "state", selected / "instance")
        shutil.copytree(relocated / "experiments", selected / "data")
        relocated_env = {key: value for key, value in env.items() if key not in ("MANAGER_STATE_DIR", "MANAGER_DATA_ROOT")}
        started = run("start.sh", relocated, relocated_env, ("--storage-dir", str(selected)))
        assert started.returncode == 0, started.stdout + started.stderr
        status = run("status.sh", relocated, relocated_env)
        assert status.returncode == 0 and str(selected / "data") in status.stdout
        configured = subprocess.run([sys.executable, "-c", "from app import STATE_DIR, DATA_ROOT; print(STATE_DIR); print(DATA_ROOT)"],
                                    cwd=relocated, env=relocated_env, capture_output=True, text=True, timeout=10)
        assert configured.returncode == 0 and str(selected / "instance") in configured.stdout
        assert str(selected / "data") in configured.stdout
        assert len(list((selected / "data").rglob("retained.txt"))) == 2
        gateway.shutdown()
        gateway.server_close()
        assert run("status.sh", relocated, relocated_env).returncode == 1
        assert run("stop.sh", relocated, relocated_env).returncode == 0
        default_start = run("start.sh", relocated, relocated_env)
        assert default_start.returncode != 0  # No database in the project's default instance/.
        assert f"Management data directory: {relocated / 'instance'}" in default_start.stdout
        assert len(list((selected / "data").rglob("retained.txt"))) == 2
    finally:
        if relocated is not None and relocated_env is not None:
            run("stop.sh", relocated, relocated_env)
        if manager_pid:
            (root / "state/gunicorn.pid").write_text(str(manager_pid))
        run("stop.sh")
        python("from app import app, SharedProject, TrackioUser, stop_trackio;\nwith app.app_context():\n for backend in [*SharedProject.query.all(), *TrackioUser.query.all()]: stop_trackio(backend)")
        if unrelated is not None:
            unrelated.terminate()
            unrelated.wait(timeout=5)
        gateway.shutdown()
        gateway.server_close()
