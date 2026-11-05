from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import app as manager
import data_transfer as transfer
from app import app, db, init_db, create_user, TrackioUser, SharedProject, ProjectMember, start_trackio, stop_trackio
from conftest import login, post


@pytest.fixture
def accounts(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(manager, 'DATA_ROOT', tmp_path / 'data')
    monkeypatch.setattr(transfer, 'DATA_ROOT', tmp_path / 'data')
    return {name: create_user(name, 'test-password-123', admin=name == 'admin')
            for name in ('admin', 'alice', 'bob')}


def client_for(name):
    client = app.test_client()
    assert login(client, name).status_code == 302
    return client


def test_self_nickname_permissions_and_invitation_display(accounts):
    alice, admin = client_for('alice'), client_for('admin')
    assert post(alice, '/profile', nickname=' 小爱 ', username='changed').status_code == 302
    assert accounts['alice'].nickname == '小爱' and accounts['alice'].username == 'alice'
    assert accounts['bob'].nickname == ''
    assert 'alice (小爱)' in alice.get('/profile').text
    assert post(alice, f"/admin/users/{accounts['bob'].id}/edit", username='renamed').status_code == 403
    assert alice.post('/profile', data={'nickname': 'unsafe'}).status_code == 400
    assert post(alice, '/profile', nickname='x' * 65).status_code == 400
    assert accounts['alice'].nickname == '小爱'
    assert post(admin, '/add', username='pending', nickname='新人').status_code == 200
    assert TrackioUser.query.filter_by(username='pending').one().nickname == '新人'
    post(admin, '/projects', name='display-test')
    project = SharedProject.query.one()
    url = f'/projects/{project.id}'
    assert '<option value="alice">alice (小爱)</option>' in admin.get(url).text
    assert 'value="pending"' not in admin.get(url).text
    post(admin, url + '/members', username='alice', role='editor')
    assert 'alice (小爱)' in admin.get(url).text
    assert post(alice, '/profile', nickname='').status_code == 302
    assert accounts['alice'].display_name == 'alice'


def test_admin_renames_self_and_other_preserving_data_and_membership(accounts):
    admin, alice = client_for('admin'), client_for('alice')
    post(alice, '/projects', name='retained-team')
    project = SharedProject.query.one()
    post(alice, f'/projects/{project.id}/members', username='bob', role='editor')
    member_token = db.session.get(ProjectMember, (project.id, accounts['bob'].id)).token
    old = Path(accounts['alice'].data_dir)
    Path(project.data_dir).mkdir(parents=True)
    (old / 'personal.txt').write_text('personal')
    (Path(project.data_dir) / 'team.txt').write_text('team')
    alias = manager.DATA_ROOT / '_shared' / project.id
    alias.parent.mkdir()
    alias.symlink_to(project.data_dir, target_is_directory=True)
    original = accounts['alice'].id, accounts['alice'].password_hash, accounts['alice'].write_token
    result = post(admin, f"/admin/users/{accounts['alice'].id}/edit", username='alice-new', nickname='爱丽丝')
    assert result.status_code == 302
    user = accounts['alice']
    assert (user.id, user.password_hash, user.write_token) == original
    assert user.nickname == '爱丽丝'
    assert not old.exists()
    assert (Path(user.data_dir) / 'personal.txt').read_text() == 'personal'
    assert (Path(project.data_dir) / 'team.txt').read_text() == 'team'
    assert (alias / 'team.txt').read_text() == 'team'
    assert db.session.get(ProjectMember, (project.id, accounts['bob'].id)).token == member_token
    assert alice.get(f'/projects/{project.id}').status_code == 200
    assert login(app.test_client(), 'alice').status_code == 401
    assert login(app.test_client(), 'alice-new').status_code == 302
    assert post(admin, f"/admin/users/{accounts['admin'].id}/edit", username='root-admin', nickname='站长').status_code == 302
    assert admin.get('/admin/users').status_code == 200
    assert login(app.test_client(), 'root-admin').status_code == 302


def test_rename_validation_and_rollback(accounts):
    admin = client_for('admin')
    user = accounts['alice']
    old = Path(user.data_dir)
    old.mkdir(parents=True)
    (old / 'retained.txt').write_text('retained')
    url = f'/admin/users/{user.id}/edit'
    for username in ('../escape', '', 'bob', 'x' * 65):
        assert post(admin, url, username=username).status_code == 400
        assert user.username == 'alice'
    destination = manager.DATA_ROOT / 'reserved'
    destination.mkdir()
    assert post(admin, url, username='reserved').status_code == 400
    with patch.object(db.session, 'commit', side_effect=SQLAlchemyError('test failure')):
        assert post(admin, url, username='alice-new', nickname='new').status_code == 400
    assert user.username == 'alice' and user.nickname == ''
    assert (old / 'retained.txt').read_text() == 'retained'
    assert not (manager.DATA_ROOT / 'alice-new').exists()


def test_rename_requires_stopped_instances_but_nickname_does_not(accounts):
    admin = client_for('admin')
    user = accounts['alice']
    start_trackio(user)
    try:
        assert post(admin, f'/admin/users/{user.id}/edit', username='alice-new').status_code == 400
        assert post(admin, f'/admin/users/{user.id}/edit', username='alice', nickname='Running user').status_code == 302
        assert manager.is_running(user)
    finally:
        stop_trackio(user)
    alice = client_for('alice')
    post(alice, '/projects', name='running-team')
    project = SharedProject.query.one()
    start_trackio(project)
    try:
        assert post(admin, f'/admin/users/{user.id}/edit', username='alice-new').status_code == 400
    finally:
        stop_trackio(project)


def test_existing_database_nickname_migration(accounts):
    db.session.execute(text('ALTER TABLE trackio_user DROP COLUMN nickname'))
    db.session.commit()
    init_db()
    init_db()
    db.session.expire_all()
    assert TrackioUser.query.count() == 3
    assert accounts['alice'].nickname == ''
    assert login(app.test_client(), 'alice').status_code == 302


def test_nickname_transfer_and_legacy_backup(accounts, tmp_path, monkeypatch):
    import json
    user = accounts['alice']
    user.nickname = '迁移昵称'
    db.session.commit()
    backup = tmp_path / 'backup'
    transfer.export_user('alice', backup)
    assert json.loads((backup / 'user.json').read_text())['user']['nickname'] == '迁移昵称'
    db.session.remove()
    db.drop_all()
    init_db()
    monkeypatch.setattr(transfer, 'DATA_ROOT', tmp_path / 'target')
    imported = transfer.import_user(backup)
    assert imported.nickname == '迁移昵称'
    db.session.remove()
    db.drop_all()
    init_db()
    manifest = json.loads((backup / 'user.json').read_text())
    del manifest['user']['nickname']
    (backup / 'user.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(transfer, 'DATA_ROOT', tmp_path / 'legacy-target')
    assert transfer.import_user(backup).nickname == ''
