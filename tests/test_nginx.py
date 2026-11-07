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
        start_trackio(alice)
        assert "private-project" in a.predict(api_name="/get_all_projects")
        check_shared_project(public, users, tmp_path)
    finally:
        for proc in reversed(procs):
            proc.terminate()
            proc.wait(timeout=10)
        stop_trackio(alice)
        stop_trackio(bob)


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
        response = owner.post(public + path + "/instance", data={
            "csrf_token": csrf(owner, path), "action": "start",
        }, timeout=45)
        assert response.status_code == 200
        shared_example = html.unescape(re.search(r"<code>(import tracknest.*?)</code>", response.text, re.S)[1])
        assert "x_axis=iteration" in response.text
        db.session.expire_all()
        alice_token = db.session.get(ProjectMember, (project_id, users["alice"].id)).token
        bob_token = db.session.get(ProjectMember, (project_id, users["bob"].id)).token
        backend = public + f"/p/{project_id}/"
        assert requests.get(backend, timeout=5).status_code == 401
        assert requests.get(backend, headers={"X-Trackio-Write-Token": project.write_token}, timeout=5).status_code == 403
        for run, token in (("alice-run", alice_token), ("bob-run", bob_token)):
            code = shared_example.replace('name="baseline"', f'name="{run}"')
            subprocess.run([sys.executable, "-c", code],
                           env=training_environment(backend, token, tmp_path),
                           check=True, capture_output=True, timeout=30)
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
        sign_in(editor, "bob")
        assert editor.get(backend, timeout=5).status_code == 200
        upload = requests.post(backend + "api/upload", headers={"X-Trackio-Write-Token": bob_token},
                               files={"files": ("sample.txt", b"x" * (32 * 1024))}, timeout=10)
        assert upload.status_code == 200 and upload.json()["paths"]
        owner.post(public + path + "/members", data={
            "csrf_token": csrf(owner, path), "username": "bob", "role": "viewer",
        }, timeout=5).raise_for_status()
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
        assert rejoined_token != new_token
        assert editor.get(backend, timeout=5).status_code == 200
        assert requests.get(backend, headers={"X-Trackio-Write-Token": new_token}, timeout=5).status_code == 403
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
