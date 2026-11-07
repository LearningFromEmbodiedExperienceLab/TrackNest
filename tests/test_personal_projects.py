"""Personal project deletion uses disposable files and real pinned Trackio storage."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import pytest

import app as manager
from app import app, BASE_DIR, create_user, db, start_trackio, stop_trackio, SharedProject, ProjectMember
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


def test_share_preserves_selected_data_original_paths_and_account_tokens(accounts):
    alice, bob = accounts['alice'], accounts['bob']
    root = Path(alice.data_dir)
    (root / 'neighbor.db').write_bytes(b'neighbor')
    for folder in ('media', 'artifacts', 'traces'):
        path = root / folder / 'experiment/sample.bin'
        path.parent.mkdir(parents=True)
        path.write_bytes(folder.encode())
    tokens = {name: user.write_token for name, user in accounts.items()}
    client = client_for()
    response = post(client, '/personal/projects/share', project='experiment', username='bob', role='viewer')
    assert response.status_code == 302
    project = SharedProject.query.one()
    assert project.name == 'experiment' and project.owner_id == alice.id and not project.pid
    target = Path(project.data_dir)
    assert (root / 'experiment.db').is_symlink()
    assert (root / 'experiment.db').resolve() == target / 'experiment.db'
    assert not Path(os.readlink(root / 'experiment.db')).is_absolute()
    for folder in ('media', 'artifacts', 'traces'):
        assert (root / folder / 'experiment/sample.bin').read_bytes() == folder.encode()
        assert (target / folder / 'experiment/sample.bin').read_bytes() == folder.encode()
    assert (root / 'neighbor.db').read_bytes() == b'neighbor'
    assert {name: user.write_token for name, user in accounts.items()} == tokens
    assert db.session.get(ProjectMember, (project.id, alice.id)).token == alice.write_token
    assert db.session.get(ProjectMember, (project.id, bob.id)).token == bob.write_token
    assert db.session.get(ProjectMember, (project.id, bob.id)).role == 'viewer'
    assert db.session.get(ProjectMember, (project.id, accounts['admin'].id)) is None
    endpoint = project.backend_path + '/__tracknest/auth'
    assert app.test_client().get(endpoint, headers={'X-Trackio-Write-Token': bob.write_token}).status_code == 200
    assert app.test_client().get(endpoint, headers={'X-Trackio-Write-Token': accounts['admin'].write_token}).status_code == 403
    assert app.test_client().get('/u/alice/__tracknest/auth', headers={'X-Trackio-Write-Token': bob.write_token}).status_code == 403
    assert 'value="experiment"' not in client.get('/personal/projects').text
    assert post(client, '/personal/projects/share', project='experiment', username='bob', role='viewer').status_code == 404


@pytest.mark.parametrize('data,status', [
    ({'project': '../bob/experiment', 'username': 'bob', 'role': 'editor'}, 404),
    ({'project': 'experiment', 'username': 'alice', 'role': 'editor'}, 400),
    ({'project': 'experiment', 'username': 'missing', 'role': 'editor'}, 400),
    ({'project': 'experiment', 'username': 'bob', 'role': 'owner'}, 400),
])
def test_share_rejects_invalid_project_and_invitation_without_changing_data(accounts, data, status):
    root = Path(accounts['alice'].data_dir)
    before = (root / 'experiment.db').read_bytes()
    assert post(client_for(), '/personal/projects/share', **data).status_code == status
    assert SharedProject.query.count() == 0
    assert (root / 'experiment.db').read_bytes() == before and not (root / 'experiment.db').is_symlink()


def test_share_requires_csrf_stopped_instance_and_unique_team_name(accounts):
    client = client_for()
    data = {'project': 'experiment', 'username': 'bob', 'role': 'editor'}
    assert client.post('/personal/projects/share', data=data).status_code == 400
    with patch('app.is_running', return_value=True):
        assert post(client, '/personal/projects/share', **data).status_code == 409
    assert post(client, '/projects', name='experiment').status_code == 302
    assert post(client, '/personal/projects/share', **data).status_code == 409
    assert not (Path(accounts['alice'].data_dir) / 'experiment.db').is_symlink()


def test_share_restores_files_when_management_commit_fails(accounts):
    from sqlalchemy.exc import SQLAlchemyError
    root = Path(accounts['alice'].data_dir)
    before = (root / 'experiment.db').read_bytes()
    client = client_for()
    with patch.object(db.session, 'commit', side_effect=SQLAlchemyError('commit failed')):
        response = post(client, '/personal/projects/share', project='experiment', username='bob', role='editor')
    assert response.status_code == 500
    assert SharedProject.query.count() == 0
    assert not (root / 'experiment.db').is_symlink()
    assert (root / 'experiment.db').read_bytes() == before


def test_share_rejects_nested_symlinks_before_moving_any_data(accounts, tmp_path):
    root = Path(accounts['alice'].data_dir)
    folder = root / 'media/experiment'
    folder.mkdir(parents=True)
    outside = tmp_path / 'private.txt'
    outside.write_text('private')
    (folder / 'outside').symlink_to(outside)
    response = post(client_for(), '/personal/projects/share', project='experiment', username='bob', role='viewer')
    assert response.status_code == 500 and SharedProject.query.count() == 0
    assert not (root / 'experiment.db').is_symlink()
    assert outside.read_text() == 'private'


def test_share_helper_restores_original_files_if_alias_creation_fails(accounts):
    root = Path(accounts['alice'].data_dir)
    destination = root / 'projects/failed-share'
    before = (root / 'experiment.db').read_bytes()
    code = f'''
from pathlib import Path
from unittest.mock import patch
from personal_projects import share_project_data
original = Path.symlink_to
calls = 0
def fail_second(self, *args, **kwargs):
    global calls
    calls += 1
    if calls == 2: raise OSError('cannot create link')
    return original(self, *args, **kwargs)
with patch.object(Path, 'symlink_to', fail_second):
    try:
        share_project_data(Path({str(root)!r}), 'experiment', Path({str(destination)!r}))
    except OSError:
        pass
    else:
        raise AssertionError('Expected alias failure')
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=BASE_DIR, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not (root / 'experiment.db').is_symlink()
    assert (root / 'experiment.db').read_bytes() == before
    assert not destination.exists()


def test_real_shared_project_preserves_media_artifacts_and_original_personal_uploads(accounts, tmp_path):
    import requests
    from trackio.remote_client import RemoteClient
    alice = accounts['alice']
    root = Path(alice.data_dir)
    weights = tmp_path / 'weights.bin'
    weights.write_bytes(b'model-weights')
    environment = {key: value for key, value in os.environ.items() if not key.startswith('TRACKIO_')}
    environment['TRACKIO_DIR'] = str(root)
    code = f'''
import trackio
from PIL import Image
trackio.init(project='convert-real', name='baseline', auto_log_gpu=False, auto_log_cpu=False)
for step in range(1, 26):
    row = {{'iteration': step, 'loss': 1 / step}}
    if step == 1: row['image'] = trackio.Image(Image.new('RGB', (3, 3), 'red'))
    trackio.log(row, step=step)
artifact = trackio.Artifact('weights', type='model')
artifact.add_file({str(weights)!r})
trackio.log_artifact(artifact)
trackio.finish()
'''
    created = subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, text=True, timeout=40)
    assert created.returncode == 0, created.stderr
    owner = client_for()
    assert post(owner, '/personal/projects/share', project='convert-real', username='bob', role='viewer').status_code == 302
    project = SharedProject.query.one()
    try:
        start_trackio(alice)
        start_trackio(project)
        personal_url = f'http://127.0.0.1:{alice.port}/'
        personal = RemoteClient(personal_url, write_token=alice.write_token)
        team = RemoteClient(f'http://127.0.0.1:{project.port}/', write_token=project.write_token)
        before = personal.predict('convert-real', 'baseline', api_name='/get_logs')
        assert len(before) == 25
        assert team.predict('convert-real', 'baseline', api_name='/get_logs') == before
        assert team.predict(api_name='/get_all_projects') == ['convert-real']
        assert 'experiment' in personal.predict(api_name='/get_all_projects')
        viewer = client_for('bob')
        media = Path(project.data_dir) / 'media' / before[0]['image']['file_path']
        response = viewer.get(project.backend_path + '/file', query_string={'path': str(media)})
        assert response.status_code == 200 and response.data == media.read_bytes()
        (root / 'media/private.txt').write_text('private neighbor data')
        assert viewer.get(project.backend_path + '/file', query_string={'path': str(root / 'media/private.txt')}).status_code == 404
        blobs = [p for p in (Path(project.data_dir) / 'artifacts/convert-real/blobs/sha256').rglob('*') if p.is_file()]
        assert blobs
        for blob in blobs:
            response = viewer.get(project.backend_path + '/artifact_blob/convert-real/' + blob.name)
            assert response.status_code == 200 and response.data == blob.read_bytes()
        continued = """import trackio
trackio.init(project='convert-real', name='continued', auto_log_gpu=False, auto_log_cpu=False)
trackio.log({'loss': 0.01}, step=26)
trackio.finish()
"""
        result = subprocess.run([sys.executable, '-c', continued], env={**environment,
            'TRACKIO_DIR': str(tmp_path / 'sdk-cache'), 'TRACKIO_SERVER_URL': personal_url,
            'TRACKIO_WRITE_TOKEN': alice.write_token}, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert team.predict('convert-real', 'continued', api_name='/get_logs')[0]['loss'] == 0.01
        response = viewer.post(project.backend_path + '/api/get_logs',
                               json={'args': ['convert-real', 'continued'], 'kwargs': {}})
        assert response.status_code == 200 and response.json['data'][0]['loss'] == 0.01
        assert viewer.post(project.backend_path + '/api/delete_run', json={}).status_code == 403
        assert viewer.get('/u/alice/').status_code == 403
    finally:
        stop_trackio(alice)
        stop_trackio(project)
