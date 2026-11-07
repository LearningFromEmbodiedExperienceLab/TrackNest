"""Persistent client settings, scoped proxy bypass, and truthful upload status."""
import importlib.util
import json
import os
import sqlite3
import stat
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import BASE_DIR


@pytest.fixture
def client_module(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("tracknest", BASE_DIR / "client/tracknest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("TRACKIO_SPACE_ID", raising=False)
    return module


@pytest.mark.parametrize("running", [False, True])
def test_login_verifies_before_saving_private_config(client_module, monkeypatch, capsys, running):
    module = client_module
    response = Mock()
    response.json.return_value = {"authenticated": True, "instance_running": running}
    verify = Mock(return_value=response)
    monkeypatch.setattr(module.httpx2, "get", verify)
    remote = Mock(side_effect=AssertionError("Login must not contact the Trackio instance"))
    monkeypatch.setattr(module, "_RemoteClient", remote)
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt: "private-token")
    monkeypatch.setattr("sys.argv", ["tracknest", "login", "--server-url", "http://localhost:9000/u/alice/"])
    assert module.main() == 0
    verify.assert_called_once_with("http://localhost:9000/u/alice/__tracknest/auth",
                                  headers={"X-Trackio-Write-Token": "private-token"}, trust_env=False, timeout=10)
    remote.assert_not_called()
    path = module._config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    config = json.loads(path.read_text())
    assert config["token"] == "private-token"
    output = capsys.readouterr().out
    assert "Login successful" in output
    assert ("Instance: stopped" in output) != running
    assert "private-token" not in output
    verify.side_effect = ConnectionError("unreachable")
    assert module.main() == 1
    assert "Login failed" in capsys.readouterr().out
    assert json.loads(path.read_text()) == config


@pytest.mark.parametrize("connected", [True, False])
def test_cli_status_reports_destination_scoped_pending_data_without_writes(client_module, monkeypatch, tmp_path, capsys, connected):
    module = client_module
    config = {"server_url": "http://localhost:9000/u/alice/", "token": "private-token"}
    monkeypatch.setattr(module, "_read_config", lambda: config)
    verify = Mock(return_value={"authenticated": True, "instance_running": True},
                  side_effect=None if connected else ConnectionError("connection refused"))
    monkeypatch.setattr(module, "_verify_credentials", verify)
    cache = tmp_path / "cache"
    cache.mkdir()
    database = cache / "buffered-project.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE metrics (space_id TEXT)")
        db.executemany("INSERT INTO metrics VALUES (?)", [(config["server_url"],),
                       (config["server_url"].rstrip("/"),), ("http://other-server/u/alice/",), (None,)])
        db.execute("CREATE TABLE pending_uploads (space_id TEXT)")
        db.execute("INSERT INTO pending_uploads VALUES (?)", (config["server_url"],))
    before = database.read_bytes()
    monkeypatch.setattr(module.SQLiteStorage, "get_project_db_path", lambda project: cache / f"{project}.db")
    monkeypatch.setattr("sys.argv", ["tracknest", "status"])
    assert module.main() == (0 if connected else 1)
    output = capsys.readouterr().out
    assert "pending 2 metrics, 0 system records, 0 traces, 1 files" in output
    assert "exclude in-memory uploads" in output
    assert "private-token" not in output
    assert ("Authentication: verified" in output) == connected
    assert database.read_bytes() == before
    assert set(cache.iterdir()) == {database}
    verify.assert_called_once_with(config["server_url"], config["token"])


def test_cli_without_configuration_exits_with_login_instructions(client_module, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["tracknest", "status"])
    assert client_module.main() == 1
    output = capsys.readouterr().out
    assert "tracknest login" in output and "Traceback" not in output


def test_cli_status_does_not_claim_empty_cache_when_a_database_is_unreadable(client_module, monkeypatch, tmp_path, capsys):
    module = client_module
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": "http://localhost/u/alice/", "token": "secret"})
    monkeypatch.setattr(module, "_verify_credentials", lambda *args: {"authenticated": True, "instance_running": True})
    (tmp_path / "broken.db").write_bytes(b"not a database")
    monkeypatch.setattr(module.SQLiteStorage, "get_project_db_path", lambda project: tmp_path / f"{project}.db")
    monkeypatch.setattr("sys.argv", ["tracknest", "status"])
    assert module.main() == 1
    output = capsys.readouterr().out
    assert "Cannot inspect broken.db" in output
    assert "cache is empty" not in output


@pytest.mark.parametrize("command,expected,api", [
    (["projects"], "experiment", "/get_all_projects"),
    (["runs", "experiment"], "baseline  id=run-1", "/get_runs_for_project"),
])
def test_cli_remote_queries_do_not_modify_proxy_environment(client_module, monkeypatch, capsys, command, expected, api):
    module = client_module
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": "http://localhost/u/alice/", "token": "secret"})
    client = Mock()
    client.predict.return_value = ["experiment"] if command[0] == "projects" else [
        {"name": "baseline", "id": "run-1", "created_at": "2026-11-07T12:00:00Z"}]
    remote = Mock(return_value=client)
    monkeypatch.setattr(module, "_RemoteClient", remote)
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:29674/")
    monkeypatch.setattr("sys.argv", ["tracknest", *command])
    before = dict(os.environ)
    assert module.main() == 0
    assert expected in capsys.readouterr().out
    assert client.predict.call_args.kwargs["api_name"] == api
    assert remote.call_args.kwargs["httpx_kwargs"] == {"trust_env": False}
    assert dict(os.environ) == before


def test_only_tracknest_connection_bypasses_proxy(client_module, monkeypatch):
    module = client_module
    module._server_url = "http://localhost:9000/u/alice/"
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:29674/")
    before = dict(os.environ)
    with patch.object(module._RemoteClient, "__init__", return_value=None) as constructor:
        module._DirectClient(module._server_url)
        assert constructor.call_args.kwargs["httpx_kwargs"] == {"trust_env": False}
        for source in ("http://localhost:9000/u/bob/", "http://other.example/", "user/space"):
            module._DirectClient(source)
            assert "httpx_kwargs" not in constructor.call_args.kwargs
    assert dict(os.environ) == before


@pytest.mark.parametrize("pending,active,expected", [
    (False, False, "sync complete"), (True, False, "sync incomplete"), (False, True, "sync incomplete"),
])
def test_finish_reports_pending_data_and_active_uploads(client_module, monkeypatch, capsys, pending, active, expected):
    module = client_module
    monkeypatch.setattr(module, "_connect", lambda: {"server_url": "http://localhost/u/alice/", "token": "secret"})
    run = SimpleNamespace(project="changed-project", name="baseline", finish=Mock(),
                          _client_thread=SimpleNamespace(is_alive=lambda: active),
                          _client_lock=threading.Lock(), _queue_lock=threading.Lock(), _sending_batch=None, _queued_logs=[],
                          _queued_system_logs=[], _queued_uploads=[], _queued_alerts=[], _has_local_buffer=False)
    monkeypatch.setattr(module._sdk, "init", lambda *args, **kwargs: run)
    monkeypatch.setattr(module.SQLiteStorage, "has_pending_data", lambda project: pending)
    original_finish = run.finish
    module.init(project="changed-project").finish()
    original_finish.assert_called_once()
    output = capsys.readouterr().out
    assert expected in output
    if pending or active:
        assert ": sync complete" not in output


def test_failed_batch_does_not_report_upload_success(client_module, capsys):
    module = client_module
    client = object.__new__(module._DirectClient)
    client._tracknest = True
    with patch.object(module._RemoteClient, "predict", side_effect=ConnectionError("connection lost")):
        with pytest.raises(ConnectionError):
            client.predict(api_name="/bulk_log", logs=[{"project": "experiment"}])
    assert not module._uploaded
    assert "Upload failed" in capsys.readouterr().out


@pytest.mark.parametrize("tty,no_color,colored", [(True, False, True), (False, False, False), (True, True, False)])
def test_english_status_colors_respect_terminal_and_no_color(client_module, monkeypatch, capsys, tty, no_color, colored):
    module = client_module
    monkeypatch.setattr(module.sys.stdout, "isatty", lambda: tty)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    module._status("Connected. Token verified.", "success")
    module._status("Upload failed", "error")
    output = capsys.readouterr().out
    assert "tracknest" in output and "Connected. Token verified." in output
    assert ("\033[32m" in output) == colored
    assert ("\033[31m" in output) == colored


def check_persistent_client(public, alice, tmp_path):
    """Called by the real Nginx integration test with two fresh Python processes."""
    import subprocess
    import sys
    from trackio.remote_client import RemoteClient

    config = tmp_path / "client-config/tracknest/client.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"server_url": public + "/u/alice/", "token": alice.write_token}))
    env = {**os.environ, "XDG_CONFIG_HOME": str(config.parent.parent),
           "PYTHONPATH": str(BASE_DIR / "client"), "TRACKIO_DIR": str(tmp_path / "client-cache")}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[key] = "socks://127.0.0.1:29674/"
    for key in ("TRACKIO_SERVER_URL", "TRACKIO_WRITE_TOKEN", "TRACKIO_SPACE_ID", "NO_PROXY", "no_proxy"):
        env.pop(key, None)
    # Reproduce the native SDK's unsupported-proxy failure before a new process retries it.
    offline = '''import trackio
trackio.init(project="client-first-project", name="offline", auto_log_gpu=False, auto_log_cpu=False)
for step in range(1, 26):
    trackio.log({"iteration": step, "loss": 1 / step}, step=step)
trackio.finish()
'''
    result = subprocess.run([sys.executable, "-c", offline], env={**env,
        "TRACKIO_SERVER_URL": public + "/u/alice/", "TRACKIO_WRITE_TOKEN": alice.write_token},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "Unknown scheme" in result.stderr
    for project in ("client-first-project", "client-renamed-project"):
        code = f'''import os
import tracknest as trackio
before = dict(os.environ)
trackio.init(project={project!r}, name="baseline", auto_log_gpu=False, auto_log_cpu=False)
for step in range(1, 26):
    trackio.log({{"iteration": step, "loss": 1 / step}}, step=step)
trackio.finish()
from trackio.sqlite_storage import SQLiteStorage
assert not SQLiteStorage.has_pending_data({project!r})
assert os.environ["ALL_PROXY"] == before["ALL_PROXY"]
assert os.environ["HTTP_PROXY"] == before["HTTP_PROXY"]
assert os.environ.get("NO_PROXY") == before.get("NO_PROXY")
'''
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stderr
        assert "Connected" in result.stdout and "sync complete" in result.stdout
        count = 50 if project == "client-first-project" else 25
        assert f"uploaded {count} metric records" in result.stdout
        assert "Unknown scheme" not in result.stderr and "saved locally" not in result.stderr
    remote = RemoteClient(public + "/u/alice/", write_token=alice.write_token, httpx_kwargs={"trust_env": False})
    projects = remote.predict(api_name="/get_all_projects")
    for project in ("client-first-project", "client-renamed-project"):
        assert project in projects
        logs = remote.predict(project, "baseline", api_name="/get_logs")
        assert len(logs) == 25
        assert [row["step"] for row in logs] == list(range(1, 26))
