from unittest.mock import patch

import pytest

from app import app, db, ProjectMember, SharedProject, create_user
from conftest import login, post


@pytest.fixture
def project(users):
    client = app.test_client()
    login(client, "alice")
    response = post(client, "/projects", name="team-baseline")
    assert response.status_code == 302
    return SharedProject.query.one()


def owner_client():
    client = app.test_client()
    login(client, "alice")
    return client


def test_project_creation_isolation_and_port_reservation(users, project):
    owner = owner_client()
    page = owner.get(f"/projects/{project.id}")
    assert page.status_code == 200
    member = db.session.get(ProjectMember, (project.id, users["alice"].id))
    assert member.role == "owner"
    assert member.token.encode() in page.data
    assert project.write_token.encode() not in page.data
    assert project.port not in {u.port for u in users.values()}
    eve = create_user("eve", "test-password-123")
    assert eve.port != project.port
    for name in ("bob", "admin"):
        client = app.test_client()
        login(client, name)
        assert client.get(f"/projects/{project.id}").status_code == 403
        assert f"/projects/{project.id}".encode() not in client.get("/").data
    assert post(owner, "/projects", name="team-baseline").status_code == 400
    assert post(owner, "/projects", name="../private").status_code == 400
    assert owner.post("/projects", data={"name": "unsafe"}).status_code == 400


def test_membership_management_roles_and_credentials(users, project):
    owner = owner_client()
    url = f"/projects/{project.id}"
    assert post(owner, url + "/members", username="bob", role="editor").status_code == 302
    bob = db.session.get(ProjectMember, (project.id, users["bob"].id))
    old_token = bob.token
    assert old_token != project.write_token
    client = app.test_client()
    login(client, "bob")
    page = client.get(url)
    assert old_token.encode() in page.data
    alice = db.session.get(ProjectMember, (project.id, users["alice"].id))
    assert alice.token.encode() not in page.data
    assert post(client, url + "/members", username="admin", role="editor").status_code == 403
    assert post(client, url + "/instance", action="start").status_code == 403
    assert post(client, url + "/delete").status_code == 403
    assert post(owner, url + "/members", username="alice", role="viewer").status_code == 400
    assert post(owner, url + "/members", username="unknown", role="viewer").status_code == 400
    assert post(owner, url + "/members", username="bob", role="owner").status_code == 400
    assert post(owner, url + "/members", username="bob", role="viewer").status_code == 302
    assert bob.role == "viewer" and bob.token == old_token
    assert post(client, url + "/token").status_code == 302
    assert bob.token != old_token
    assert post(owner, url + f"/members/{users['alice'].id}/remove").status_code == 400
    assert post(owner, url + f"/members/{users['bob'].id}/remove").status_code == 302
    assert client.get(url).status_code == 403
    assert post(client, url + "/token").status_code == 403


def proxy_auth(client, project, path="/", method="GET", **headers):
    return client.get("/_trackio/auth", headers={
        "X-Original-URI": f"/p/{project.id}{path}",
        "X-Original-Path": f"/p/{project.id}{path}",
        "X-Original-Method": method,
        **headers,
    })


def test_project_gateway_roles_revocation_and_unknown_routes(users, project):
    owner = owner_client()
    post(owner, f"/projects/{project.id}/members", username="bob", role="viewer")
    bob = db.session.get(ProjectMember, (project.id, users["bob"].id))
    anonymous = app.test_client()
    viewer = app.test_client()
    login(viewer, "bob")
    with patch("app.is_running", return_value=True):
        assert proxy_auth(anonymous, project).status_code == 401
        assert proxy_auth(anonymous, project, **{"X-Trackio-Write-Token": project.write_token}).status_code == 403
        assert proxy_auth(anonymous, project, **{"X-Trackio-Write-Token": users["bob"].write_token}).status_code == 403
        response = proxy_auth(viewer, project)
        assert response.status_code == 204 and response.headers.get("X-Trackio-Token", "") == ""
        for path in ("/version", "/file", "/config.json", "/assets/app.js", "/static/trackio/logo.png"):
            assert proxy_auth(viewer, project, path).status_code == 204
        for path in ("/api/get_logs", "/api/query_project", "/api/get_all_projects"):
            assert proxy_auth(viewer, project, path, "POST").status_code == 204
        for path in ("/api/log", "/api/delete_run", "/api/rename_run", "/api/upload", "/api/force_sync", "/api/new_unknown_api"):
            assert proxy_auth(viewer, project, path, "POST").status_code == 403
            assert proxy_auth(anonymous, project, path, "POST", **{"X-Trackio-Write-Token": bob.token}).status_code == 403
        assert proxy_auth(viewer, project, "/oauth/logout").status_code == 403
        assert proxy_auth(viewer, project, "/api/delete_run", "GET").status_code == 403
        assert proxy_auth(viewer, project, Origin="https://evil.example", **{"X-External-Origin": "http://localhost"}).status_code == 403
        post(owner, f"/projects/{project.id}/members", username="bob", role="editor")
        response = proxy_auth(anonymous, project, "/api/log", "POST", **{"X-Trackio-Write-Token": bob.token})
        assert response.status_code == 204 and response.headers["X-Trackio-Token"] == project.write_token
        token = bob.token
        post(owner, f"/projects/{project.id}/members/{users['bob'].id}/remove")
        assert proxy_auth(anonymous, project, **{"X-Trackio-Write-Token": token}).status_code == 403
        assert proxy_auth(viewer, project).status_code == 403
    with patch("app.is_running", return_value=False):
        assert proxy_auth(owner, project).status_code == 403


def test_project_deletion_and_account_cleanup(users, project):
    owner = owner_client()
    post(owner, f"/projects/{project.id}/members", username="bob", role="viewer")
    admin = app.test_client()
    login(admin, "admin")
    response = post(admin, f"/delete/{users['alice'].id}")
    assert response.status_code == 302 and response.location == "/admin/users"
    assert db.session.get(type(users["alice"]), users["alice"].id) is not None
    assert db.session.get(SharedProject, project.id) is not None
    assert post(admin, f"/delete/{users['bob'].id}").status_code == 302
    assert ProjectMember.query.filter_by(user_id=users["bob"].id).count() == 0
    project_id, directory = project.id, project.data_dir
    with patch("app.start_trackio") as start:
        assert post(owner, f"/projects/{project.id}/instance", action="start").status_code == 302
        start.assert_called_once()
    with patch("app.stop_trackio") as stop:
        assert post(owner, f"/projects/{project.id}/delete").status_code == 302
        stop.assert_called_once()
    assert ProjectMember.query.filter_by(project_id=project_id).count() == 0
    assert db.session.get(SharedProject, project_id) is None
    post(owner, "/projects", name="team-baseline")
    replacement = SharedProject.query.one()
    assert replacement.id != project_id and replacement.data_dir != directory


def test_project_language_switch_stays_on_project(users, project):
    client = owner_client()
    response = post(client, "/language", language="en", next=f"/projects/{project.id}")
    assert response.location == f"/projects/{project.id}"
    page = client.get(response.location)
    assert b"Project members" in page.data
    assert b"My project credentials" in page.data


def test_project_tokens_are_scoped_even_for_members_of_both_projects(users, project):
    owner = owner_client()
    token = db.session.get(ProjectMember, (project.id, users["alice"].id)).token
    bob = app.test_client()
    login(bob, "bob")
    assert post(bob, "/projects", name=project.name).status_code == 302
    other = SharedProject.query.filter_by(owner_id=users["bob"].id).one()
    post(bob, f"/projects/{other.id}/members", username="alice", role="editor")
    with patch("app.is_running", return_value=True):
        assert proxy_auth(owner, other).status_code == 204
        assert proxy_auth(owner, other, **{"X-Trackio-Write-Token": token}).status_code == 403
    assert other.port != project.port


def test_project_port_exhaustion_does_not_create_partial_project(users, project):
    client = owner_client()
    with patch("app.get_free_port", side_effect=RuntimeError("No ports")):
        assert post(client, "/projects", name="no-capacity").status_code == 503
    assert SharedProject.query.count() == 1
    assert ProjectMember.query.count() == 1


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_registered_user_can_be_invited_and_leave(users, project, role):
    owner = owner_client()
    url = f"/projects/{project.id}"
    assert post(owner, url + "/members", username="bob", role=role).status_code == 302
    member = db.session.get(ProjectMember, (project.id, users["bob"].id))
    token = member.token
    assert member.role == role
    client = app.test_client()
    login(client, "bob")
    assert url.encode() in client.get("/").data
    assert client.get(url).status_code == 200
    state = (project.pid, project.port, project.write_token, project.data_dir)
    with patch("app.stop_trackio") as stop:
        # A submitted user ID cannot make this endpoint remove someone else.
        response = post(client, url + "/leave", user_id=users["alice"].id)
        assert response.status_code == 302 and response.location == "/"
        stop.assert_not_called()
    assert db.session.get(ProjectMember, (project.id, users["bob"].id)) is None
    assert db.session.get(ProjectMember, (project.id, users["alice"].id)).role == "owner"
    assert (project.pid, project.port, project.write_token, project.data_dir) == state
    assert url.encode() not in client.get("/").data
    assert client.get(url).status_code == 403
    assert post(client, url + "/token").status_code == 403
    assert client.get("/").status_code == 200
    with patch("app.is_running", return_value=True):
        assert proxy_auth(client, project).status_code == 403
        assert proxy_auth(app.test_client(), project, **{"X-Trackio-Write-Token": token}).status_code == 403
        assert proxy_auth(owner, project).status_code == 204
        post(owner, url + "/members", username="bob", role=role)
        replacement = db.session.get(ProjectMember, (project.id, users["bob"].id))
        assert replacement.token != token
        assert proxy_auth(app.test_client(), project, **{"X-Trackio-Write-Token": token}).status_code == 403
        assert proxy_auth(client, project).status_code == 204


def test_leave_requires_membership_csrf_and_preserves_owner(users, project):
    owner = owner_client()
    url = f"/projects/{project.id}/leave"
    assert owner.get(url).status_code == 405
    assert owner.post(url).status_code == 400
    assert post(owner, url).status_code == 400
    outsider = app.test_client()
    login(outsider, "bob")
    assert post(outsider, url).status_code == 403
    assert db.session.get(ProjectMember, (project.id, users["alice"].id)).role == "owner"


def test_project_data_is_created_under_its_owner(users, project):
    from pathlib import Path
    assert Path(project.data_dir) == Path(users["alice"].data_dir) / "projects" / project.id


def test_legacy_project_data_migration_preserves_files_and_credentials(users, project):
    from pathlib import Path
    from app import DATA_ROOT

    legacy = DATA_ROOT / "_shared" / project.id
    legacy.mkdir(parents=True)
    (legacy / "media").mkdir()
    (legacy / "media" / "example.txt").write_text("retained experiment media")
    project.data_dir = str(legacy)
    db.session.commit()
    token, port = project.write_token, project.port
    with patch("app.is_running", return_value=True), patch("app.stop_trackio") as stop, patch("app.start_trackio") as start:
        result = app.test_cli_runner().invoke(args=["migrate-project-data"])
        assert result.exit_code == 0, result.output
        stop.assert_called_once_with(project)
        start.assert_called_once_with(project)
    destination = Path(users["alice"].data_dir) / "projects" / project.id
    assert project.data_dir == str(destination)
    assert (destination / "media" / "example.txt").read_text() == "retained experiment media"
    # Absolute media paths already stored by Trackio remain usable.
    assert legacy.is_symlink() and legacy.resolve() == destination
    assert (legacy / "media" / "example.txt").read_text() == "retained experiment media"
    assert (project.write_token, project.port) == (token, port)
    assert app.test_cli_runner().invoke(args=["migrate-project-data"]).exit_code == 0


def test_project_data_migration_does_not_overwrite_existing_directory(users, project):
    from pathlib import Path
    from app import DATA_ROOT

    legacy = DATA_ROOT / "_shared" / project.id
    legacy.mkdir(parents=True)
    (legacy / "original.txt").write_text("original")
    project.data_dir = str(legacy)
    db.session.commit()
    destination = Path(users["alice"].data_dir) / "projects" / project.id
    destination.mkdir(parents=True)
    (destination / "existing.txt").write_text("existing")
    with patch("app.stop_trackio") as stop:
        result = app.test_cli_runner().invoke(args=["migrate-project-data"])
        assert result.exit_code != 0 and str(destination) in result.output
        stop.assert_not_called()
    assert project.data_dir == str(legacy)
    assert (legacy / "original.txt").read_text() == "original"
    assert (destination / "existing.txt").read_text() == "existing"


def test_admin_deletes_owned_projects_only_after_all_confirmations(users, project):
    from pathlib import Path
    from app import TrackioUser

    owner = owner_client()
    for name in ("bbb", "ccc"):
        post(owner, "/projects", name=name)
    projects = SharedProject.query.filter_by(owner_id=users["alice"].id).order_by(SharedProject.name).all()
    bob = app.test_client()
    login(bob, "bob")
    post(bob, "/projects", name="unrelated")
    other = SharedProject.query.filter_by(owner_id=users["bob"].id).one()
    post(bob, f"/projects/{other.id}/members", username="alice", role="editor")
    post(owner, f"/projects/{project.id}/members", username="bob", role="viewer")
    for item in projects:
        directory = Path(item.data_dir)
        directory.mkdir(parents=True)
        (directory / "retained.txt").write_text("experiment data")
    admin = app.test_client()
    login(admin, "admin")
    page = admin.get("/admin/users")
    assert all(item.name in page.text for item in projects)
    assert "admin_users.js" in page.text
    for confirmed in ([], [projects[0].id], [item.id for item in projects] + [other.id]):
        with patch("app.stop_trackio") as stop:
            response = post(admin, f"/delete/{users['alice'].id}", confirmed_project_ids=confirmed)
            assert response.status_code == 302
            stop.assert_not_called()
        assert SharedProject.query.filter_by(owner_id=users["alice"].id).count() == 3
        assert "请逐一确认" in admin.get(response.location).text
    uid = users["alice"].id
    with patch("app.stop_trackio") as stop:
        response = post(admin, f"/delete/{uid}", confirmed_project_ids=[item.id for item in projects])
        assert response.status_code == 302
        assert {call.args[0] for call in stop.call_args_list} == {*projects, users["alice"]}
    assert db.session.get(TrackioUser, uid) is None
    assert SharedProject.query.filter_by(owner_id=uid).count() == 0
    assert ProjectMember.query.filter_by(user_id=uid).count() == 0
    assert ProjectMember.query.filter_by(project_id=project.id).count() == 0
    assert db.session.get(SharedProject, other.id) is not None
    assert all((Path(item.data_dir) / "retained.txt").read_text() == "experiment data" for item in projects)


def test_account_project_deletion_requires_admin_and_csrf(users, project):
    from app import TrackioUser

    owner = owner_client()
    url = f"/delete/{users['alice'].id}"
    assert post(owner, url, confirmed_project_ids=[project.id]).status_code == 403
    admin = app.test_client()
    login(admin, "admin")
    assert admin.post(url, data={"confirmed_project_ids": [project.id]}).status_code == 400
    assert post(admin, f"/delete/{users['admin'].id}", confirmed_project_ids=[project.id]).status_code == 400
    assert db.session.get(TrackioUser, users["alice"].id) is not None
    assert db.session.get(SharedProject, project.id) is not None
    admin.set_cookie("tracknest_language", "en")
    page = post(admin, "/add", username="pending")
    assert page.status_code == 200
    assert 'id="copy-invitation"' in page.text
    page = admin.get("/admin/users")
    assert "admin_users.js" in page.text and project.name in page.text
    assert "This user owns these team projects" in page.text
    assert project.write_token not in page.text
