import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest
import requests
from trackio.remote_client import RemoteClient

import app as manager
import data_transfer as transfer
from app import app, db, init_db, create_user, TrackioUser, SharedProject, ProjectMember, start_trackio, stop_trackio
from conftest import login, post


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    root = tmp_path / "source data"
    monkeypatch.setattr(manager, "DATA_ROOT", root)
    monkeypatch.setattr(transfer, "DATA_ROOT", root)


@pytest.fixture
def project(users):
    client = app.test_client()
    login(client, "alice")
    post(client, "/projects", name="team-test")
    project = SharedProject.query.one()
    post(client, f"/projects/{project.id}/members", username="bob", role="editor")
    directory = Path(project.data_dir)
    directory.mkdir(parents=True)
    (directory / "retained.txt").write_text("team experiment")
    (Path(users["alice"].data_dir) / "personal.txt").write_text("personal experiment")
    return project


def fresh_target(monkeypatch, path):
    db.session.remove()
    db.drop_all()
    init_db()
    monkeypatch.setattr(manager, "DATA_ROOT", path)
    monkeypatch.setattr(transfer, "DATA_ROOT", path)
    create_user("bob", "target-password-123")
    create_user("admin", "target-password-123", admin=True)


def test_whole_deployment_relocation_preserves_accounts_and_files(users, project, tmp_path, monkeypatch):
    source = transfer.DATA_ROOT
    alias = source / "_shared" / project.id
    alias.parent.mkdir()
    alias.symlink_to(project.data_dir, target_is_directory=True)
    credentials = [(user.id, user.password_hash, user.write_token, user.port) for user in users.values()]
    members = [(member.project_id, member.user_id, member.role, member.token) for member in ProjectMember.query.all()]
    project.pid = 2147483647
    users["alice"].pid = 2147483647
    db.session.commit()
    target = tmp_path / "new machine" / "data"
    shutil.copytree(source, target, symlinks=True)
    monkeypatch.setattr(transfer, "DATA_ROOT", target)
    result = app.test_cli_runner().invoke(args=["relocate-data"])
    assert result.exit_code == 0, result.output
    assert [(user.id, user.password_hash, user.write_token, user.port) for user in users.values()] == credentials
    assert [(member.project_id, member.user_id, member.role, member.token) for member in ProjectMember.query.all()] == members
    assert all(user.data_dir == str(target / user.username) and user.pid == 0 for user in users.values())
    assert project.data_dir == str(target / "alice/projects" / project.id) and project.pid == 0
    assert (Path(project.data_dir) / "retained.txt").read_text() == "team experiment"
    relocated_alias = target / "_shared" / project.id
    assert not Path(os.readlink(relocated_alias)).is_absolute()
    assert relocated_alias.resolve() == Path(project.data_dir)
    assert app.test_cli_runner().invoke(args=["relocate-data"]).exit_code == 0
    assert (source / "alice/projects" / project.id / "retained.txt").exists()


def test_relocation_requires_data_and_stopped_instances(users, project, tmp_path, monkeypatch):
    original = project.data_dir
    target = tmp_path / "missing data"
    monkeypatch.setattr(transfer, "DATA_ROOT", target)
    assert app.test_cli_runner().invoke(args=["relocate-data"]).exit_code != 0
    export = app.test_cli_runner().invoke(args=["export-user", "alice", str(tmp_path / "backup")])
    assert export.exit_code != 0 and "relocate-data" in export.output
    assert not (tmp_path / "backup").exists()
    target.mkdir()
    with patch("data_transfer.is_running", return_value=True):
        result = app.test_cli_runner().invoke(args=["relocate-data"])
        assert result.exit_code != 0 and "先停止" in result.output
    assert project.data_dir == original


def test_single_user_transfer_reassigns_ids_and_preserves_owned_data(users, project, tmp_path, monkeypatch):
    source_uid = users["alice"].id
    project_id = project.id
    owner_token = db.session.get(ProjectMember, (project_id, source_uid)).token
    user_token = users["alice"].write_token
    package = tmp_path / "user backup"
    source = Path(users["alice"].data_dir)
    (source / "personal-link.txt").symlink_to(source / "personal.txt")
    result = app.test_cli_runner().invoke(args=["export-user", "alice", str(package)])
    assert result.exit_code == 0, result.output
    assert package.stat().st_mode & 0o777 == 0o700
    assert (package / "user.json").stat().st_mode & 0o777 == 0o600
    assert not Path(os.readlink(package / "data/personal-link.txt")).is_absolute()
    assert "bob" not in (package / "user.json").read_text()
    fresh_target(monkeypatch, tmp_path / "target data")
    result = app.test_cli_runner().invoke(args=["import-user", str(package)])
    assert result.exit_code == 0, result.output
    imported = TrackioUser.query.filter_by(username="alice").one()
    assert imported.id != source_uid and imported.pid == 0 and not imported.is_admin
    assert imported.write_token == user_token
    assert login(app.test_client(), "alice").status_code == 302
    imported_project = db.session.get(SharedProject, project_id)
    assert imported_project.owner_id == imported.id
    assert imported_project.data_dir == str(Path(imported.data_dir) / "projects" / project_id)
    assert ProjectMember.query.filter_by(project_id=project_id).count() == 1
    assert db.session.get(ProjectMember, (project_id, imported.id)).token == owner_token
    assert (Path(imported_project.data_dir) / "retained.txt").read_text() == "team experiment"
    assert (Path(imported.data_dir) / "personal-link.txt").read_text() == "personal experiment"
    ports = [user.port for user in TrackioUser.query.all()] + [imported_project.port]
    assert len(set(ports)) == len(ports)
    assert app.test_cli_runner().invoke(args=["import-user", str(package)]).exit_code != 0


def test_transfer_rejects_running_users_existing_data_and_invalid_packages(users, project, tmp_path, monkeypatch):
    package = tmp_path / "backup"
    with patch("data_transfer.is_running", return_value=True):
        assert app.test_cli_runner().invoke(args=["export-user", "alice", str(package)]).exit_code != 0
    assert not package.exists()
    source = Path(users["alice"].data_dir)
    (source / "outside-link").symlink_to(tmp_path / "outside")
    assert app.test_cli_runner().invoke(args=["export-user", "alice", str(package)]).exit_code != 0
    assert not package.exists()
    (source / "outside-link").unlink()
    assert app.test_cli_runner().invoke(args=["export-user", "alice", str(package)]).exit_code == 0
    assert app.test_cli_runner().invoke(args=["export-user", "alice", str(package)]).exit_code != 0
    assert app.test_cli_runner().invoke(args=["import-user", str(package)]).exit_code != 0
    fresh_target(monkeypatch, tmp_path / "target data")
    retained = transfer.DATA_ROOT / "alice"
    retained.mkdir(parents=True)
    (retained / "keep.txt").write_text("existing")
    assert app.test_cli_runner().invoke(args=["import-user", str(package)]).exit_code != 0
    assert (retained / "keep.txt").read_text() == "existing"
    shutil.rmtree(retained)
    manifest = json.loads((package / "user.json").read_text())
    manifest["user"]["username"] = "../outside"
    (package / "user.json").write_text(json.dumps(manifest))
    assert app.test_cli_runner().invoke(args=["import-user", str(package)]).exit_code != 0
    assert not (tmp_path / "outside").exists()


def test_real_trackio_curves_and_media_survive_copy_and_user_import(users, project, tmp_path, monkeypatch):
    from PIL import Image

    project_id = project.id
    running = project
    try:
        start_trackio(project)
        client = RemoteClient(f"http://127.0.0.1:{project.port}/", write_token=project.write_token)
        training = '''
import trackio
from PIL import Image
trackio.init(project="team-test", name="baseline")
for iteration in range(1,26):
    row={"iteration":iteration,"loss":1/iteration}
    if iteration==1:row["image"]=trackio.Image(Image.new("RGB",(3,3),"red"))
    trackio.log(row,step=iteration)
trackio.finish()
'''
        result = subprocess.run([sys.executable, "-c", training], env={
            **os.environ, "TRACKIO_DIR": str(tmp_path / "sdk cache"),
            "TRACKIO_SERVER_URL": f"http://127.0.0.1:{project.port}/", "TRACKIO_WRITE_TOKEN": project.write_token,
        }, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stderr
        before = client.predict("team-test", "baseline", api_name="/get_logs")
        assert len(before) == 25 and before[0]["image"]["file_path"]
        stop_trackio(project)
        copied = tmp_path / "copied data"
        shutil.copytree(transfer.DATA_ROOT, copied, symlinks=True)
        monkeypatch.setattr(transfer, "DATA_ROOT", copied)
        assert app.test_cli_runner().invoke(args=["relocate-data"]).exit_code == 0
        start_trackio(project)
        assert client.predict("team-test", "baseline", api_name="/get_logs") == before
        media = Path(project.data_dir) / "media" / before[0]["image"]["file_path"]
        assert Image.open(media).convert("RGB").getpixel((0, 0)) == (255, 0, 0)
        response = requests.get(f"http://127.0.0.1:{project.port}/file", params={"path": str(media)},
                                headers={"X-Trackio-Write-Token": project.write_token}, timeout=5)
        assert response.status_code == 200 and response.content == media.read_bytes()
        stop_trackio(project)
        package = tmp_path / "portable alice"
        assert app.test_cli_runner().invoke(args=["export-user", "alice", str(package)]).exit_code == 0
        fresh_target(monkeypatch, tmp_path / "another machine/data")
        assert app.test_cli_runner().invoke(args=["import-user", str(package)]).exit_code == 0
        running = db.session.get(SharedProject, project_id)
        start_trackio(running)
        imported = RemoteClient(f"http://127.0.0.1:{running.port}/", write_token=running.write_token)
        assert imported.predict("team-test", "baseline", api_name="/get_logs") == before
        media = Path(running.data_dir) / "media" / before[0]["image"]["file_path"]
        assert Image.open(media).convert("RGB").getpixel((0, 0)) == (255, 0, 0)
        response = requests.get(f"http://127.0.0.1:{running.port}/file", params={"path": str(media)},
                                headers={"X-Trackio-Write-Token": running.write_token}, timeout=5)
        assert response.status_code == 200 and response.content == media.read_bytes()
    finally:
        stop_trackio(running)


def test_import_project_conflicts_and_partial_failure_do_not_leave_an_account(users, project, tmp_path, monkeypatch):
    package = tmp_path / "backup"
    project_id = project.id
    assert app.test_cli_runner().invoke(args=["export-user", "alice", str(package)]).exit_code == 0
    fresh_target(monkeypatch, tmp_path / "target data")
    bob = TrackioUser.query.filter_by(username="bob").one()
    conflict = SharedProject(id=project_id, name="existing", owner=bob, port=manager.get_free_port(),
                             write_token="existing-token", data_dir=str(transfer.DATA_ROOT / "bob/projects" / project_id))
    db.session.add(conflict)
    db.session.commit()
    assert app.test_cli_runner().invoke(args=["import-user", str(package)]).exit_code != 0
    assert db.session.get(SharedProject, project_id).owner_id == bob.id
    assert not (transfer.DATA_ROOT / "alice").exists()
    db.session.delete(conflict)
    db.session.commit()
    with patch("data_transfer.get_free_port", side_effect=[manager.get_free_port(), RuntimeError("端口池耗尽")]):
        failed = app.test_cli_runner().invoke(args=["import-user", str(package)])
        assert failed.exit_code != 0 and "端口池耗尽" in failed.output
    assert TrackioUser.query.filter_by(username="alice").first() is None
    assert not (transfer.DATA_ROOT / "alice").exists()
    assert app.test_cli_runner().invoke(args=["import-user", str(package)]).exit_code == 0
