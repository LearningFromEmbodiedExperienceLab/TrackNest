"""Personal project deletion uses disposable files and real pinned Trackio storage."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import pytest

import app as manager
from app import app, BASE_DIR, create_user, db, start_trackio, stop_trackio
from conftest import login, post


@pytest.fixture
def accounts(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(manager, 'DATA_ROOT', tmp_path / 'data')
    users = {name: create_user(name, 'test-password-123', admin=name == 'admin') for name in ('alice', 'bob', 'admin')}
    for user in users.values():
        root = Path(user.data_dir)
        root.mkdir(parents=True)
        with sqlite3.connect(root / 'experiment.db') as connection:
            connection.execute('CREATE TABLE retained (value TEXT)')
            connection.execute('INSERT INTO retained VALUES (?)', (user.username,))
    return users


def client_for(username='alice'):
    client = app.test_client()
    assert login(client, username).status_code == 302
    return client


def helper(root, project):
    return subprocess.run([sys.executable, str(BASE_DIR / 'personal_projects.py'), str(root), project],
                          env={**os.environ, 'TRACKIO_DIR': str(root)}, capture_output=True, text=True, timeout=30)


def test_listing_is_private_and_top_level_only(accounts):
    alice = client_for()
    root = Path(accounts['alice'].data_dir)
    with sqlite3.connect(root / 'registry-models.db'):
        pass
    team = root / 'projects/team'
    team.mkdir(parents=True)
    (team / 'team-only.db').touch()
    (Path(accounts['bob'].data_dir) / 'bob-only.db').touch()
    (root / 'media').mkdir()
    (root / 'media/hidden.db').touch()
    page = alice.get('/personal/projects')
    assert page.status_code == 200
    assert 'experiment' in page.text
    assert 'team-only' not in page.text and 'bob-only' not in page.text and 'registry-models' not in page.text
    assert 'value="hidden"' not in page.text
    assert '/personal/projects' in alice.get('/').text
    assert app.test_client().get('/personal/projects').status_code == 302


def test_delete_removes_project_data_but_keeps_other_users_and_team(accounts):
    user = accounts['alice']
    root = Path(user.data_dir)
    sidecars = ['experiment.parquet', 'experiment_system.parquet', 'experiment_configs.parquet', 'experiment_traces.parquet', 'experiment.db-wal', 'experiment.db-shm', 'experiment.hash', 'experiment.lock']
    for name in sidecars:
        (root / name).write_text('stale export')
    for name in ('media/experiment', 'artifacts/experiment', 'traces/experiment'):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / 'retained.txt').write_text('experiment data')
    team = root / 'projects/team'
    team.mkdir(parents=True)
    (team / 'experiment.db').write_text('team')
    (root / 'experiment-neighbor.db').write_text('neighbor')
    original = (user.password_hash, user.write_token, user.port)
    alice = client_for()
    response = post(alice, '/personal/projects/delete', project='experiment', confirm_name='experiment', user_id=accounts['bob'].id, data_dir=accounts['bob'].data_dir)
    assert response.status_code == 302
    assert response.headers['Location'] == '/personal/projects'
    assert not (root / 'experiment.db').exists()
    assert all(not (root / name).exists() for name in sidecars)
    assert all(not (root / name).exists() for name in ('media/experiment', 'artifacts/experiment', 'traces/experiment'))
    assert (team / 'experiment.db').read_text() == 'team'
    assert (root / 'experiment-neighbor.db').read_text() == 'neighbor'
    for name in ('bob', 'admin'):
        with sqlite3.connect(Path(accounts[name].data_dir) / 'experiment.db') as connection:
            assert connection.execute('SELECT value FROM retained').fetchone()[0] == name
    assert (user.password_hash, user.write_token, user.port) == original


def test_delete_requires_session_csrf_exact_confirmation_and_existing_name(accounts):
    alice = client_for()
    root = Path(accounts['alice'].data_dir)
    url = '/personal/projects/delete'
    assert alice.get(url).status_code == 405
    assert alice.post(url, data={'project': 'experiment', 'confirm_name': 'experiment'}).status_code == 400
    guest = app.test_client()
    guest.get('/login')
    assert post(guest, url, project='experiment', confirm_name='experiment').status_code == 302
    for confirmation in ('', 'other', 'experiment '):
        assert post(alice, url, project='experiment', confirm_name=confirmation).status_code == 400
    for project in ('missing', '../bob/experiment', '/tmp/experiment', 'experiment.db', 'ex.periment'):
        assert post(alice, url, project=project, confirm_name=project).status_code == 404
    assert (root / 'experiment.db').exists()


def test_running_instance_blocks_deletion_but_stopping_allows_it(accounts):
    alice = client_for()
    user = accounts['alice']
    start_trackio(user)
    try:
        page = alice.get('/personal/projects')
        assert page.status_code == 200 and 'disabled' in page.text
        assert post(alice, '/personal/projects/delete', project='experiment', confirm_name='experiment').status_code == 409
        assert (Path(user.data_dir) / 'experiment.db').exists()
    finally:
        stop_trackio(user)
    assert post(alice, '/personal/projects/delete', project='experiment', confirm_name='experiment').status_code == 302


def test_symlink_ancestor_rejected_before_any_deletion(accounts, tmp_path):
    root = Path(accounts['alice'].data_dir)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'experiment').mkdir()
    (outside / 'experiment/keep.txt').write_text('outside')
    (root / 'media').symlink_to(outside, target_is_directory=True)
    result = helper(root, 'experiment')
    assert result.returncode != 0
    assert (root / 'experiment.db').exists()
    assert (outside / 'experiment/keep.txt').read_text() == 'outside'


def test_empty_space_and_failed_cleanup_are_not_reported_as_success(accounts):
    alice = client_for()
    root = Path(accounts['alice'].data_dir)
    (root / 'experiment.db').unlink()
    assert alice.get('/personal/projects').status_code == 200
    (root / 'experiment.db').touch()
    failure = subprocess.CompletedProcess([], 1, '', 'failed')
    with patch('app.subprocess.run', return_value=failure):
        response = post(alice, '/personal/projects/delete', project='experiment', confirm_name='experiment')
    assert response.status_code == 500
    assert (root / 'experiment.db').exists()


def test_state_is_refreshed_after_acquiring_the_instance_lock(accounts, monkeypatch):
    from contextlib import contextmanager
    from sqlalchemy import text
    user = accounts['alice']
    alice = client_for()

    @contextmanager
    def start_won_the_lock_first():
        with db.engine.begin() as connection:
            connection.execute(text('UPDATE trackio_user SET pid=42 WHERE id=:id'), {'id': user.id})
        yield

    monkeypatch.setattr(manager, 'instance_lock', start_won_the_lock_first)
    monkeypatch.setattr(manager, 'is_running', lambda backend: backend.pid == 42)
    assert post(alice, '/personal/projects/delete', project='experiment', confirm_name='experiment').status_code == 409
    assert (Path(user.data_dir) / 'experiment.db').exists()


def test_cleanup_failure_reports_error_and_does_not_report_success(accounts):
    root = Path(accounts['alice'].data_dir)
    media = root / 'media/experiment'
    media.mkdir(parents=True)
    (media / 'keep.txt').write_text('not removable')
    media.chmod(0o500)
    try:
        response = post(client_for(), '/personal/projects/delete', project='experiment', confirm_name='experiment')
        assert response.status_code == 500
        assert '部分文件可能已删除' in response.text
        assert (media / 'keep.txt').exists()
    finally:
        media.chmod(0o700)


@pytest.mark.parametrize('target', ['experiment', 'experiment_system'])
def test_helper_preserves_sidecar_owned_by_a_legacy_neighbor(accounts, target):
    root = Path(accounts['alice'].data_dir)
    # Older imported datasets may contain names now reserved by Trackio.
    (root / 'experiment_system.db').write_text('other project')
    (root / 'experiment_system.parquet').write_text('other project export')
    result = helper(root, target)
    assert result.returncode != 0
    assert (root / 'experiment.db').exists()
    assert (root / 'experiment_system.db').exists()
    assert (root / 'experiment_system.parquet').read_text() == 'other project export'


def test_helper_rejects_symlink_into_a_team_before_deleting(accounts):
    root = Path(accounts['alice'].data_dir)
    team = root / 'projects/team'
    team.mkdir(parents=True)
    (team / 'keep.txt').write_text('team data')
    (root / 'artifacts').mkdir()
    (root / 'artifacts/experiment').symlink_to(team, target_is_directory=True)
    assert helper(root, 'experiment').returncode != 0
    assert (root / 'experiment.db').exists()
    assert (team / 'keep.txt').read_text() == 'team data'


def test_language_switch_stays_on_personal_project_page(accounts):
    alice = client_for()
    assert alice.get('/personal/projects').status_code == 200
    response = post(alice, '/language', language='en', next='/personal/projects')
    assert response.headers['Location'] == '/personal/projects'
    assert 'Personal experiment projects' in alice.get('/personal/projects').text


def test_real_25_point_trackio_experiment_can_be_deleted(accounts):
    root = Path(accounts['alice'].data_dir)
    environment = {key: value for key, value in os.environ.items() if not key.startswith('TRACKIO_')}
    environment['TRACKIO_DIR'] = str(root)
    code = '''
import trackio
trackio.init(project='real-experiment', name='curve', auto_log_gpu=False, auto_log_cpu=False, capture_console=False)
for iteration in range(1, 26):
    trackio.log({'iteration': iteration, 'loss': 1.0 / iteration}, step=iteration)
trackio.finish()
'''
    created = subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, text=True, timeout=30)
    assert created.returncode == 0, created.stderr
    database = root / 'real-experiment.db'
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT COUNT(*) FROM metrics').fetchone()[0] == 25
    alice = client_for()
    assert 'real-experiment' in alice.get('/personal/projects').text
    response = post(alice, '/personal/projects/delete', project='real-experiment', confirm_name='real-experiment')
    assert response.status_code == 302
    assert not database.exists()
    assert 'value="real-experiment"' not in alice.get('/personal/projects').text
    assert (root / 'experiment.db').exists()
