"""Optional full-stack test: NGINX_BIN=/path/to/nginx python -m pytest."""
import html
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest
import requests
from trackio.remote_client import RemoteClient

from app import BASE_DIR, start_trackio, stop_trackio


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            requests.get(url, timeout=1)
            return
        except requests.RequestException:
            time.sleep(0.1)
    raise AssertionError(f"Server did not become ready: {url}")


def training_environment(backend, token, tmp_path):
    root = Path(tempfile.mkdtemp(dir=tmp_path))
    config = root / "tracknest/client.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"server_url": backend, "token": token}))
    return {**os.environ, "XDG_CONFIG_HOME": str(root), "PYTHONPATH": str(BASE_DIR / "client")}


def test_nginx_login_ingestion_and_isolation(users, tmp_path):
    binary = os.environ.get("NGINX_BIN") or shutil.which("nginx")
    if not binary:
        pytest.skip("Set NGINX_BIN to run the real Nginx integration test")
    manager_port, public_port = free_port(), free_port()
    config = (BASE_DIR / "nginx/manager.conf").read_text()
    config = config.replace("127.0.0.1:9001", f"127.0.0.1:{manager_port}")
    config = config.replace("listen 9000", f"listen 127.0.0.1:{public_port}")
    config = f"""daemon off;
master_process off;
pid {tmp_path}/nginx.pid;
error_log {tmp_path}/error.log;
events {{}}
http {{
    access_log off;
    client_body_temp_path {tmp_path}/body;
    proxy_temp_path {tmp_path}/proxy;
    fastcgi_temp_path {tmp_path}/fastcgi;
    uwsgi_temp_path {tmp_path}/uwsgi;
    scgi_temp_path {tmp_path}/scgi;
    {config}
}}
"""
    conf = tmp_path / "nginx.conf"
    conf.write_text(config)
    procs = []
    alice, bob = users["alice"], users["bob"]
    public = f"http://127.0.0.1:{public_port}"
    try:
        start_trackio(alice)
        start_trackio(bob)
        with open(tmp_path / "manager.log", "w") as log:
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "gunicorn", "-w", "1", "--threads", "4", "-b", f"127.0.0.1:{manager_port}", "app:app"],
                cwd=BASE_DIR, stdout=log, stderr=log,
            ))
        wait_for(f"http://127.0.0.1:{manager_port}/login")
        checked = subprocess.run([binary, "-t", "-c", str(conf), "-p", str(tmp_path)], capture_output=True)
        assert checked.returncode == 0, checked.stderr.decode()
        procs.append(subprocess.Popen([binary, "-c", str(conf), "-p", str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        wait_for(f"{public}/login")
        from test_client_distribution import check_wheel_install
        check_wheel_install(public, tmp_path)
        with requests.Session() as browser:
            browser.trust_env = False
            assert browser.get(f"{public}/u/alice/").status_code == 401
            assert browser.get(f"{public}/_trackio/auth").status_code == 404
            page = browser.get(f"{public}/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
            page = browser.post(f"{public}/login", data={"csrf_token": csrf, "username": "alice", "password": "test-password-123"})
            assert page.status_code == 200
            assert f"{public}/u/alice/" in page.text
            training_example = html.unescape(re.search(r"<code>(import tracknest.*?)</code>", page.text, re.S)[1])
            page = browser.get(f"{public}/u/alice/")
            assert page.status_code == 200
            assert 'window.__trackio_base = "/u/alice"' in page.text
            scripts = re.findall(r'<script[^>]+src="([^"]+)"', page.text)
            assert scripts
            assert all(browser.get(f"{public}{src}").status_code == 200 for src in scripts if src.startswith("/"))
            assert browser.get(f"{public}/u/bob/").status_code == 403
            assert browser.get(f"{public}/u/bob/api/get_all_projects").status_code == 403
            assert browser.post(f"{public}/u/alice/api/get_all_projects", json={"args": [], "kwargs": {}}, headers={"Origin": "https://evil.example"}).status_code == 403
            assert browser.post(f"{public}/u/alice/api/get_all_projects", json={"args": [], "kwargs": {}}, headers={"Origin": public}).status_code == 200
            # A spoofed routing header cannot select a different user's backend.
            assert browser.get(f"{public}/u/bob/", headers={"X-Original-URI": "/u/alice/"}).status_code == 403
        wrong = {"X-Trackio-Write-Token": alice.write_token}
        assert requests.get(f"{public}/u/bob/version", headers=wrong, timeout=5).status_code == 403
        subprocess.run(
            [sys.executable, "-c", training_example.replace('project="my-project"', 'project="private-project"')],
            env=training_environment(f"{public}/u/alice/", alice.write_token, tmp_path),
            check=True, capture_output=True, timeout=30,
        )
        a = RemoteClient(f"{public}/u/alice/", write_token=alice.write_token)
        b = RemoteClient(f"{public}/u/bob/", write_token=bob.write_token)
        assert "private-project" in a.predict(api_name="/get_all_projects")
        assert "private-project" not in b.predict(api_name="/get_all_projects")
        logs = a.predict("private-project", "baseline", api_name="/get_logs")
        assert len(logs) == 25
        assert [row["iteration"] for row in logs] == list(range(1, 26))
        assert [row["step"] for row in logs] == list(range(1, 26))
        assert logs[-1]["loss"] == pytest.approx(1 / 25)
        assert logs[-1]["accuracy"] == pytest.approx(1 - 1 / 26)
        from test_client import check_persistent_client
        check_persistent_client(public, alice, tmp_path)
        stop_trackio(alice)
        check_login_while_stopped(public, alice, tmp_path)
        from app import db, is_running
        db.session.refresh(alice)
        assert is_running(alice)
        assert "private-project" in a.predict(api_name="/get_all_projects")
        check_shared_project(public, users, tmp_path)
        check_personal_project_sharing(public, users, tmp_path)
    finally:
        for proc in reversed(procs):
            proc.terminate()
            proc.wait(timeout=10)
        stop_trackio(alice)
        stop_trackio(bob)


def check_login_while_stopped(public, alice, tmp_path):
    environment = training_environment(f"{public}/u/alice/", alice.write_token, tmp_path)
    environment["TEST_LOGIN_TOKEN"] = alice.write_token
    code = f'''import os, sys, tracknest
tracknest.getpass.getpass = lambda prompt: os.environ["TEST_LOGIN_TOKEN"]
sys.argv = ["tracknest", "login", "--server-url", "{public}/u/alice/"]
raise SystemExit(tracknest.main())
'''
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "Login successful" in result.stdout and "Instance: stopped" in result.stdout
    config = Path(environment["XDG_CONFIG_HOME"]) / "tracknest/client.json"
    before = config.read_bytes()
    environment["TEST_LOGIN_TOKEN"] = "invalid-token"
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1 and "Login successful" not in result.stdout
    assert "Login failed" in result.stdout
    assert config.read_bytes() == before
    result = subprocess.run([sys.executable, "-m", "tracknest", "status"], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert re.search(r"Authentication:\s+verified", result.stdout) and re.search(r"Instance:\s+stopped", result.stdout)
    for language in ("en", "zh"):
        result = subprocess.run([sys.executable, "-m", "tracknest", "projects", "--lang", language], env=environment,
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 1
        assert "tracknest start" in result.stdout and "Space" not in result.stdout and "503" not in result.stdout
        assert ("实例尚未启动" in result.stdout) == (language == "zh")
    for command, expected in (("start", "running"), ("start", "running"), ("stop", "stopped"), ("stop", "stopped")):
        result = subprocess.run([sys.executable, "-m", "tracknest", command], env=environment,
                                capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"Instance: {expected}." in result.stdout
    code = '''import tracknest as trackio
trackio.init(project="auto-start", name="baseline", auto_log_gpu=False, auto_log_cpu=False)
trackio.log({"loss": 0.25}, step=1)
trackio.finish()
'''
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Starting it before training" in result.stdout and "sync complete" in result.stdout
    remote = RemoteClient(public + "/u/alice/", write_token=alice.write_token, httpx_kwargs={"trust_env": False})
    logs = remote.predict("auto-start", "baseline", api_name="/get_logs")
    assert len(logs) == 1 and logs[0]["loss"] == 0.25


def check_personal_project_sharing(public, users, tmp_path):
    from app import db, SharedProject
    browser = requests.Session()
    browser.trust_env = False
    page = browser.get(public + '/login', timeout=5)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    page = browser.post(public + '/login', data={'csrf_token': csrf, 'username': 'alice',
                                               'password': 'test-password-123'}, timeout=5)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
    data = {'csrf_token': csrf, 'project': 'private-project', 'username': 'bob', 'role': 'viewer'}
    assert browser.post(public + '/personal/projects/share', data=data, timeout=10).status_code == 409
    browser.post(public + f'/stop/{users["alice"].id}', data={'csrf_token': csrf}, timeout=10).raise_for_status()
    page = browser.post(public + '/personal/projects/share', data=data, timeout=15)
    assert page.status_code == 200
    project_id = page.url.rsplit('/', 1)[1]
    db.session.expire_all()
    project = db.session.get(SharedProject, project_id)
    assert project and project.name == 'private-project'
    old_url, old_token = public + '/u/alice/', users['alice'].write_token
    try:
        browser.post(public + f'/projects/{project_id}/instance', data={'csrf_token': csrf, 'action': 'start'}, timeout=45).raise_for_status()
        browser.post(public + f'/start/{users["alice"].id}', data={'csrf_token': csrf}, timeout=45).raise_for_status()
        team = RemoteClient(public + project.backend_path + '/', write_token=users['bob'].write_token)
        assert team.predict(api_name='/get_all_projects') == ['private-project']
        assert len(team.predict('private-project', 'baseline', api_name='/get_logs')) == 25
        continued = '''import tracknest as trackio
trackio.init(project="private-project", name="after-sharing", auto_log_gpu=False, auto_log_cpu=False)
trackio.log({"loss":0.01}, step=26)
trackio.finish()
'''
        result = subprocess.run([sys.executable, '-c', continued],
                                env=training_environment(old_url, old_token, tmp_path),
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert team.predict('private-project', 'after-sharing', api_name='/get_logs')[0]['loss'] == 0.01
        headers = {'X-Trackio-Write-Token': users['bob'].write_token}
        assert requests.post(public + project.backend_path + '/api/delete_run', headers=headers, json={}, timeout=5).status_code == 403
        assert requests.get(old_url, headers=headers, timeout=5).status_code == 403
    finally:
        db.session.refresh(project)
        stop_trackio(project)
        db.session.expire_all()
        browser.close()


def check_shared_project(public, users, tmp_path):
    from app import db, ProjectMember, SharedProject

    def csrf(browser, path):
        page = browser.get(public + path, timeout=5)
        assert page.status_code == 200
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]

    def sign_in(browser, username):
        response = browser.post(public + "/login", data={
            "csrf_token": csrf(browser, "/login"), "username": username, "password": "test-password-123",
        }, timeout=5)
        assert response.status_code == 200

    owner, editor = requests.Session(), requests.Session()
    owner.trust_env = editor.trust_env = False
    project = None
    try:
        sign_in(owner, "alice")
        response = owner.post(public + "/projects", data={"csrf_token": csrf(owner, "/"), "name": "shared-training"}, timeout=5)
        assert response.status_code == 200
        path = response.url.removeprefix(public)
        project_id = path.rsplit("/", 1)[1]
        db.session.expire_all()
        project = db.session.get(SharedProject, project_id)
        assert project
        response = owner.post(public + path + "/members", data={
            "csrf_token": csrf(owner, path), "username": "bob", "role": "editor",
        }, timeout=5)
        assert response.status_code == 200
        response = owner.get(public + path, timeout=5)
        assert response.status_code == 200
        shared_example = html.unescape(re.search(r"<code>(import tracknest.*?)</code>", response.text, re.S)[1])
        db.session.expire_all()
        alice_token = db.session.get(ProjectMember, (project_id, users["alice"].id)).token
        bob_token = db.session.get(ProjectMember, (project_id, users["bob"].id)).token
        backend = public + f"/p/{project_id}/"
        assert requests.get(backend, timeout=5).status_code == 401
        assert requests.get(backend, headers={"X-Trackio-Write-Token": project.write_token}, timeout=5).status_code == 403
        for run, token in (("bob-run", bob_token), ("alice-run", alice_token)):
            code = shared_example.replace('name="baseline"', f'name="{run}"')
            result = subprocess.run([sys.executable, "-c", code],
                                    env=training_environment(backend, token, tmp_path),
                                    capture_output=True, text=True, timeout=45)
            assert result.returncode == 0, result.stdout + result.stderr
            assert ("Starting it before training" in result.stdout) == (run == "bob-run")
        assert "x_axis=iteration" in owner.get(public + path, timeout=5).text
        shared = RemoteClient(backend, write_token=bob_token)
        assert len(shared.predict("shared-training", api_name="/get_runs_for_project")) == 2
        for run in ("alice-run", "bob-run"):
            logs = shared.predict("shared-training", run, api_name="/get_logs")
            assert len(logs) == 25
            assert [row["iteration"] for row in logs] == list(range(1, 26))
            assert [row["step"] for row in logs] == list(range(1, 26))
            assert logs[-1]["loss"] == pytest.approx(1 / 25)
            assert logs[-1]["accuracy"] == pytest.approx(1 - 1 / 26)
        assert "private-project" not in shared.predict(api_name="/get_all_projects")
        personal = RemoteClient(public + "/u/alice/", write_token=users["alice"].write_token)
        assert "shared-training" not in personal.predict(api_name="/get_all_projects")
        owner_env = training_environment(public + "/u/alice/", alice_token, tmp_path)
        editor_env = training_environment(public + "/u/bob/", bob_token, tmp_path)

        def instance_cli(command, environment, selector="shared-training"):
            config = Path(environment["XDG_CONFIG_HOME"]) / "tracknest/client.json"
            before = config.read_bytes()
            result = subprocess.run([sys.executable, "-m", "tracknest", command, "--project", selector],
                                    env=environment, capture_output=True, text=True, timeout=45)
            assert config.read_bytes() == before
            return result

        result = instance_cli("stop", owner_env, project_id)
        assert result.returncode == 0, result.stdout + result.stderr
        assert requests.get(backend + "__tracknest/auth", headers={"X-Trackio-Write-Token": alice_token}, timeout=5).json()["instance_running"] is False
        assert requests.get(public + "/u/alice/__tracknest/auth", headers={"X-Trackio-Write-Token": alice_token}, timeout=5).json()["instance_running"] is True
        result = instance_cli("status", owner_env)
        assert result.returncode == 1 and "tracknest start --project shared-training" in result.stdout
        result = instance_cli("start", editor_env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert instance_cli("stop", editor_env).returncode == 1
        assert instance_cli("status", editor_env).returncode == 0
        sign_in(editor, "bob")
        assert editor.get(backend, timeout=5).status_code == 200
        upload = requests.post(backend + "api/upload", headers={"X-Trackio-Write-Token": bob_token},
                               files={"files": ("sample.txt", b"x" * (32 * 1024))}, timeout=10)
        assert upload.status_code == 200 and upload.json()["paths"]
        owner.post(public + path + "/members", data={
            "csrf_token": csrf(owner, path), "username": "bob", "role": "viewer",
        }, timeout=5).raise_for_status()
        assert instance_cli("status", editor_env).returncode == 0
        assert instance_cli("start", editor_env).returncode == 1
        assert instance_cli("stop", editor_env).returncode == 1
        assert shared.predict("shared-training", "alice-run", api_name="/get_logs")[0]["loss"] == 1.0
        assert shared.predict(api_name="/get_run_mutation_status")["allowed"] is False
        logo = shared.predict(api_name="/get_settings")["logo_urls"]["light"]
        assert logo.startswith(f"/p/{project_id}/static/trackio/")
        assert editor.get(public + logo, timeout=5).status_code == 200
        assert requests.get(public + "/static/trackio/trackio_logo_light.png", timeout=5).status_code == 200
        page = editor.get(backend, timeout=5)
        assert page.status_code == 200
        for src in re.findall(r'(?:src|href)="([^\"]+)"', page.text):
            if src.startswith(f"/p/{project_id}/"):
                assert editor.get(public + src, timeout=5).status_code == 200
        for api in ("delete_run", "rename_run", "upload", "bulk_log", "bulk_upload_media", "force_sync", "unknown"):
            headers = {"X-Trackio-Write-Token": bob_token, "X-Original-Method": "GET", "X-Original-Path": f"/p/{project_id}/api/get_logs"}
            assert requests.post(backend + "api/" + api, headers=headers, json={"args": [], "kwargs": {}}, timeout=5).status_code == 403
            assert editor.post(backend + "api/" + api, json={"args": [], "kwargs": {}}, timeout=5).status_code == 403
        assert requests.post(backend + "api/%64elete_run", headers={"X-Trackio-Write-Token": bob_token}, json={}, timeout=5).status_code == 403
        assert editor.post(backend + "api/get_all_projects", headers={"Origin": "https://evil.example"}, json={}, timeout=5).status_code == 403
        editor.post(public + path + "/token", data={"csrf_token": csrf(editor, path)}, timeout=5).raise_for_status()
        db.session.expire_all()
        new_token = db.session.get(ProjectMember, (project_id, users["bob"].id)).token
        assert new_token != bob_token
        assert requests.get(backend, headers={"X-Trackio-Write-Token": bob_token}, timeout=5).status_code == 403
        assert requests.get(backend, headers={"X-Trackio-Write-Token": new_token}, timeout=5).status_code == 200
        left = editor.post(public + path + "/leave", data={"csrf_token": csrf(editor, path)}, timeout=5)
        assert left.status_code == 200 and left.url == public + "/"
        assert path not in left.text
        assert editor.get(public + path, timeout=5).status_code == 403
        assert editor.get(backend, timeout=5).status_code == 403
        assert requests.get(backend, headers={"X-Trackio-Write-Token": new_token}, timeout=5).status_code == 403
        assert RemoteClient(backend, write_token=alice_token).predict("shared-training", "bob-run", api_name="/get_logs")[0]["loss"] == 1.0
        owner.post(public + path + "/members", data={
            "csrf_token": csrf(owner, path), "username": "bob", "role": "editor",
        }, timeout=5).raise_for_status()
        db.session.expire_all()
        rejoined_token = db.session.get(ProjectMember, (project_id, users["bob"].id)).token
        assert rejoined_token == new_token
        assert editor.get(backend, timeout=5).status_code == 200
        assert requests.get(backend, headers={"X-Trackio-Write-Token": new_token}, timeout=5).status_code == 200
        owner.post(public + path + f"/members/{users['bob'].id}/remove", data={"csrf_token": csrf(owner, path)}, timeout=5).raise_for_status()
        assert editor.get(backend, timeout=5).status_code == 403
        assert requests.get(backend, headers={"X-Trackio-Write-Token": rejoined_token}, timeout=5).status_code == 403
        db.session.expire_all()
        stop_trackio(project)
        start_trackio(project)
        assert RemoteClient(backend, write_token=alice_token).predict("shared-training", "bob-run", api_name="/get_logs")[0]["loss"] == 1.0
        directory = Path(project.data_dir)
        owner.post(public + path + "/delete", data={"csrf_token": csrf(owner, path)}, timeout=10).raise_for_status()
        assert directory.exists()
        assert requests.get(backend, headers={"X-Trackio-Write-Token": alice_token}, timeout=5).status_code == 403
        project = None
    finally:
        if project is not None:
            db.session.expire_all()
            stop_trackio(project)
        owner.close()
        editor.close()
