"""Persistent client settings, scoped proxy bypass, and truthful upload status."""
import importlib.util
import json
import os
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


def test_login_verifies_before_saving_private_config(client_module, monkeypatch, capsys):
    module = client_module
    remote = Mock()
    monkeypatch.setattr(module, "_RemoteClient", remote)
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt: "private-token")
    monkeypatch.setattr("sys.argv", ["tracknest", "login", "--server-url", "http://localhost:9000/u/alice/"])
    module.main()
    remote.assert_called_once_with("http://localhost:9000/u/alice/", write_token="private-token",
                                   httpx_kwargs={"trust_env": False}, verbose=False)
    path = module._config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    config = json.loads(path.read_text())
    assert config["token"] == "private-token"
    assert "private-token" not in capsys.readouterr().out
    remote.side_effect = ConnectionError("unreachable")
    with pytest.raises(ConnectionError):
        module.main()
    assert json.loads(path.read_text()) == config


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
    (False, False, "同步完成"), (True, False, "同步未完成"), (False, True, "同步未完成"),
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
        assert "：同步完成" not in output


def test_failed_batch_does_not_report_upload_success(client_module, capsys):
    module = client_module
    client = object.__new__(module._DirectClient)
    client._tracknest = True
    with patch.object(module._RemoteClient, "predict", side_effect=ConnectionError("connection lost")):
        with pytest.raises(ConnectionError):
            client.predict(api_name="/bulk_log", logs=[{"project": "experiment"}])
    assert not module._uploaded
    assert "上传失败" in capsys.readouterr().out


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
        assert "已连接" in result.stdout and "同步完成" in result.stdout
        count = 50 if project == "client-first-project" else 25
        assert f"已上传 {count} 条指标" in result.stdout
        assert "Unknown scheme" not in result.stderr and "saved locally" not in result.stderr
    remote = RemoteClient(public + "/u/alice/", write_token=alice.write_token, httpx_kwargs={"trust_env": False})
    projects = remote.predict(api_name="/get_all_projects")
    for project in ("client-first-project", "client-renamed-project"):
        assert project in projects
        logs = remote.predict(project, "baseline", api_name="/get_logs")
        assert len(logs) == 25
        assert [row["step"] for row in logs] == list(range(1, 26))
