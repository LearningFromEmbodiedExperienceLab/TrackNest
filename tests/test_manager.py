from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text
from werkzeug.security import check_password_hash

from app import app, create_user, db, init_db, is_running, start_trackio, stop_trackio, TrackioUser
from conftest import login, post


def test_login_and_private_dashboard(users):
    client = app.test_client()
    assert client.get("/").status_code == 302
    assert login(client, "alice").status_code == 302
    page = client.get("/")
    assert page.status_code == 200
    assert users["alice"].write_token.encode() in page.data
    assert users["bob"].write_token.encode() not in page.data
    assert users["admin"].write_token.encode() not in page.data
    assert b"/u/bob/" not in page.data
    assert page.headers["Cache-Control"] == "no-store"
    assert users["alice"].password_hash != "test-password-123"
    assert check_password_hash(users["alice"].password_hash, "test-password-123")
    post(client, "/logout")
    assert client.get("/").status_code == 302


def test_invalid_login_and_csrf(users):
    client = app.test_client()
    client.get("/login")
    assert client.post("/login", data={"username": "alice"}).status_code == 400
    assert post(client, "/login", username="alice", password="wrong").status_code == 401
    assert client.get("/").status_code == 302


@pytest.mark.parametrize("action", ["start", "stop", "delete"])
def test_cannot_manage_another_user(users, action):
    client = app.test_client()
    login(client, "alice")
    with patch("app.start_trackio") as start, patch("app.stop_trackio") as stop:
        assert post(client, f"/{action}/{users['bob'].id}").status_code == 403
        start.assert_not_called()
        stop.assert_not_called()
    assert client.get(f"/{action}/{users['bob'].id}").status_code == 405


def test_admin_can_create_and_manage_without_seeing_other_tokens(users):
    client = app.test_client()
    login(client, "alice")
    assert post(client, "/add", username="eve", password="test-password-123").status_code == 403
    login(client, "admin")
    assert post(client, "/add", username="eve").status_code == 200
    eve = TrackioUser.query.filter_by(username="eve").one()
    assert eve.port not in [u.port for u in users.values()]
    assert users["alice"].write_token.encode() not in client.get("/").data
    assert eve.password_hash is None
    with patch("app.start_trackio") as start:
        assert post(client, f"/start/{eve.id}").status_code == 403
        start.assert_not_called()
    assert post(client, f"/delete/{users['admin'].id}").status_code == 400


@pytest.mark.parametrize("username", ["../outside", "a/b", "a;return 200;", "_invalid", "", "x" * 65])
def test_rejects_unsafe_usernames(ctx, username):
    with pytest.raises(ValueError):
        create_user(username, "test-password-123")
    assert TrackioUser.query.count() == 0


def test_short_password_and_duplicate_user(users):
    with pytest.raises(ValueError):
        create_user("eve", "short")
    with pytest.raises(ValueError):
        create_user("alice", "test-password-123")


def auth(client, name, **headers):
    return client.get("/_trackio/auth", headers={"X-Original-URI": f"/u/{name}/api/get_projects", **headers})


def test_proxy_authorization_for_browser_and_sdk(users):
    client = app.test_client()
    with patch("app.is_running", return_value=True):
        assert auth(client, "alice").status_code == 401
        assert auth(client, "alice", **{"X-Trackio-Write-Token": users["bob"].write_token}).status_code == 403
        assert auth(client, "alice", **{"X-Trackio-Write-Token": users["alice"].write_token}).status_code == 204
        login(client, "alice")
        assert auth(client, "alice").status_code == 204
        assert auth(client, "bob").status_code == 403
        assert auth(client, "bob", **{"X-Trackio-Write-Token": users["alice"].write_token}).status_code == 403
        assert auth(client, "alice", Origin="https://evil.example", **{"X-External-Origin": "http://localhost"}).status_code == 403
        assert auth(client, "alice", **{"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.get("/_trackio/auth").status_code == 403
        login(client, "admin")
        assert auth(client, "alice").status_code == 403
    with patch("app.is_running", return_value=False):
        response = auth(client, "alice", **{"X-Trackio-Write-Token": users["alice"].write_token})
        assert response.status_code == 503
        assert "实验实例未运行".encode() in response.data
        assert auth(client, "alice", **{"X-Trackio-Write-Token": users["bob"].write_token}).status_code == 403


def test_stale_pid_never_signaled(users):
    import os
    user = users["alice"]
    user.pid = os.getpid()
    assert not is_running(user)
    with patch("app.os.killpg") as kill:
        stop_trackio(user)
        kill.assert_not_called()
    assert user.pid == 0


def test_legacy_database_migration(ctx):
    db.drop_all()
    db.session.execute(text("""CREATE TABLE trackio_user (
        id INTEGER PRIMARY KEY, username VARCHAR(64) NOT NULL UNIQUE,
        write_token VARCHAR(64) NOT NULL, port INTEGER NOT NULL UNIQUE,
        pid INTEGER, data_dir VARCHAR(128) NOT NULL)"""))
    db.session.execute(text("INSERT INTO trackio_user VALUES (1, 'legacy', 'old-token', 7860, 0, '/tmp/legacy')"))
    db.session.commit()
    init_db()
    legacy = db.session.get(TrackioUser, 1)
    assert not legacy.password_hash and not legacy.is_admin
    assert legacy.write_token == "old-token"
    assert login(app.test_client(), "legacy").status_code == 401


def test_real_backends_use_separate_directories(users):
    import requests
    import os
    import subprocess
    import sys
    alice, bob = users["alice"], users["bob"]
    try:
        for user in (alice, bob):
            start_trackio(user)
            assert is_running(user)
            html = requests.get(f"http://127.0.0.1:{user.port}/", timeout=5).text
            assert f'window.__trackio_base = "/u/{user.username}"' in html
        subprocess.run(
            [sys.executable, "-c", 'import trackio; trackio.init(project="alice-only", name="run"); trackio.log({"loss": 0.25}); trackio.finish()'],
            env={**os.environ, "TRACKIO_SERVER_URL": f"http://127.0.0.1:{alice.port}/", "TRACKIO_WRITE_TOKEN": alice.write_token},
            check=True, capture_output=True, timeout=30,
        )
        assert list(Path(alice.data_dir).rglob("*.db"))
        assert not list(Path(bob.data_dir).rglob("*.db"))
    finally:
        stop_trackio(alice)
        stop_trackio(bob)


def test_start_and_dashboard_ignore_environment_proxies(users, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    alice = users["alice"]
    client = app.test_client()
    login(client, "alice")
    try:
        assert post(client, f"/start/{alice.id}").status_code == 302
        db.session.refresh(alice)
        assert is_running(alice)
        page = client.get("/")
        assert f'href="/u/{alice.username}/?x_axis=iteration"'.encode() in page.data
        assert client.get(f"/u/{alice.username}/").status_code == 200
        assert client.post(f"/u/{alice.username}/api/get_all_projects",
                           json={"args": [], "kwargs": {}}).status_code == 200
    finally:
        stop_trackio(alice)


def test_language_switch_persists_across_login_and_logout(users):
    client = app.test_client()
    page = client.get("/login")
    assert b'lang="zh-CN"' in page.data
    response = post(client, "/language", language="en", next="/login")
    assert response.location == "/login"
    page = client.get("/login")
    assert b'lang="en"' in page.data
    assert b"Enter your username" in page.data
    failed = post(client, "/login", username="alice", password="wrong")
    assert failed.status_code == 401
    assert b"Incorrect username or password" in failed.data
    login(client, "admin")
    page = client.get("/")
    assert b"My panel" in page.data
    assert b"User management" in page.data
    assert b"Generate invitation link" in client.get("/admin/users").data
    post(client, "/logout")
    assert b'lang="en"' in client.get("/login").data
    post(client, "/language", language="zh", next="/login")
    assert "回到你的实验空间".encode() in client.get("/login").data


def test_language_switch_requires_csrf_and_rejects_external_redirect(ctx):
    client = app.test_client()
    client.get("/login")
    assert client.post("/language", data={"language": "en"}).status_code == 400
    assert post(client, "/language", language="invalid").status_code == 400
    response = post(client, "/language", language="en", next="https://example.com/")
    assert response.location == "/"


def test_migrate_ports_preserves_accounts_and_running_state(users):
    from app import PORT_END, PORT_START

    alice, bob = users["alice"], users["bob"]
    alice.port, bob.port = 7860, 7861
    db.session.commit()
    original = {u.id: (u.write_token, u.data_dir) for u in users.values()}
    admin_port = users["admin"].port
    with patch("app.is_running", side_effect=lambda user: user.username == "bob"), patch("app.stop_trackio"), patch("app.start_trackio") as start:
        result = app.test_cli_runner().invoke(args=["migrate-ports"])
        assert result.exit_code == 0, result.output
        assert start.call_count == 1
        assert start.call_args.args[0].id == bob.id
    db.session.expire_all()
    migrated = TrackioUser.query.all()
    assert all(PORT_START <= u.port <= PORT_END for u in migrated)
    assert len({u.port for u in migrated}) == len(migrated)
    assert users["admin"].port == admin_port
    assert {u.id: (u.write_token, u.data_dir) for u in migrated} == original
    again = app.test_cli_runner().invoke(args=["migrate-ports"])
    assert again.exit_code == 0 and not again.output


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
@pytest.mark.parametrize("path", ["/", "/login", "/u/admin/", "/p/0123456789abcdef0123456789abcdef/?project=team"])
def test_direct_backend_access_redirects_to_public_entry(ctx, host, path):
    response = app.test_client().get(path, base_url=f"http://{host}:9001")
    assert response.status_code == 307
    assert response.location == f"http://{host}:9000{path}"


def test_backend_redirect_preserves_form_method_and_public_entry_does_not_loop(ctx):
    client = app.test_client()
    response = client.post("/login", base_url="http://localhost:9001", data={"username": "alice"})
    assert response.status_code == 307
    assert response.location == "http://localhost:9000/login"
    assert client.get("/login", base_url="http://localhost:9000").status_code == 200
