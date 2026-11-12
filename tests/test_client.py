"""Persistent client settings, scoped proxy bypass, and truthful upload status."""
import importlib.util
import json
import os
import re
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
    module._load_sdk()
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
    assert [line.split() for line in output.splitlines() if line.strip().startswith("buffered-project")] == [
        ["buffered-project", "2", "0", "0", "1"]]
    assert "Pending uploads" in output and "Metrics" in output and "Total" in output
    assert "exclude in-memory uploads" in output
    assert "private-token" not in output
    assert bool(re.search(r"Authentication:\s+verified", output)) == connected
    assert database.read_bytes() == before
    assert set(cache.iterdir()) == {database}
    verify.assert_called_once_with(config["server_url"], config["token"])


def test_cli_without_configuration_exits_with_login_instructions(client_module, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["tracknest", "status"])
    assert client_module.main() == 1
    output = capsys.readouterr().out
    assert "tracknest login" in output and "Traceback" not in output


@pytest.mark.parametrize("action,running", [("start", True), ("stop", False)])
def test_cli_manages_configured_instance(client_module, monkeypatch, capsys, action, running):
    module = client_module
    config = {"server_url": "http://localhost:9000/u/alice/", "token": "private-token"}
    monkeypatch.setattr(module, "_read_config", lambda: config)
    response = Mock(status_code=200)
    response.json.return_value = {"instance_running": running}
    manage = Mock(return_value=response)
    monkeypatch.setattr(module.httpx2, "post", manage)
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:29674/")
    monkeypatch.setattr("sys.argv", ["tracknest", action])
    before = dict(os.environ)
    assert module.main() == 0
    manage.assert_called_once_with("http://localhost:9000/u/alice/__tracknest/instance",
                                   headers={"X-Trackio-Write-Token": "private-token"},
                                   json={"action": action}, trust_env=False, timeout=60)
    output = capsys.readouterr().out
    assert ("Instance: running." if running else "Instance: stopped.") in output
    assert "private-token" not in output
    assert dict(os.environ) == before


@pytest.mark.parametrize("status,state", [(503, {}), (200, {"instance_running": False})])
def test_cli_start_failure_does_not_report_success(client_module, monkeypatch, capsys, status, state):
    module = client_module
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": "http://localhost/u/alice/", "token": "secret"})
    response = Mock(status_code=status)
    response.json.return_value = state
    monkeypatch.setattr(module.httpx2, "post", Mock(return_value=response))
    monkeypatch.setattr("sys.argv", ["tracknest", "start"])
    assert module.main() == 1
    assert "Instance: running." not in capsys.readouterr().out


@pytest.mark.parametrize("command", ["start", "stop", "status"])
@pytest.mark.parametrize("by_id", [False, True])
@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("saved_backend", ["/u/alice/", "/p/" + "c" * 32 + "/"])
def test_cli_targets_team_instance_without_changing_saved_connection(client_module, monkeypatch, tmp_path, capsys,
                                                                     command, by_id, language, saved_backend):
    module = client_module
    project_id = "a" * 32
    config = {"server_url": "http://localhost:9000" + saved_backend, "token": "private-token"}
    path = module._config_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    listing = Mock()
    listing.json.return_value = {"projects": [{"id": project_id, "name": "team-baseline", "owner": "alice"}]}
    get = Mock(return_value=listing)
    monkeypatch.setattr(module.httpx2, "get", get)
    response = Mock(status_code=200)
    response.json.return_value = {"instance_running": command != "stop"}
    post = Mock(return_value=response)
    monkeypatch.setattr(module.httpx2, "post", post)
    verify = Mock(return_value={"authenticated": True, "instance_running": True})
    monkeypatch.setattr(module, "_verify_credentials", verify)
    pending = Mock(return_value=True)
    monkeypatch.setattr(module, "_pending_status", pending)
    monkeypatch.setattr("sys.argv", ["tracknest", command, "--project", project_id if by_id else "team-baseline", "--lang", language])
    assert module.main() == 0
    target = f"http://localhost:9000/p/{project_id}/"
    get.assert_called_once_with("http://localhost:9000/__tracknest/projects",
                                headers={"X-Trackio-Write-Token": "private-token"}, trust_env=False, timeout=10)
    if command == "status":
        verify.assert_called_once_with(target, "private-token")
        pending.assert_called_once_with(target)
        post.assert_not_called()
    else:
        post.assert_called_once_with(target + "__tracknest/instance", json={"action": command},
                                     headers={"X-Trackio-Write-Token": "private-token"}, trust_env=False, timeout=60)
    output = capsys.readouterr().out
    assert target in output and "private-token" not in output
    assert path.read_bytes() == before


@pytest.mark.parametrize("projects,expected", [
    ([], "not found or you do not have access"),
    ([{"id": "a" * 32, "name": "same", "owner": "alice"},
      {"id": "b" * 32, "name": "same", "owner": "bob"}], "Use --project <ID>"),
])
def test_cli_rejects_missing_or_ambiguous_team_project_before_mutating(client_module, monkeypatch, capsys, projects, expected):
    module = client_module
    config = {"server_url": "http://localhost/p/" + "c" * 32 + "/", "token": "private-token"}
    monkeypatch.setattr(module, "_read_config", lambda: config)
    response = Mock()
    response.json.return_value = {"projects": projects}
    monkeypatch.setattr(module.httpx2, "get", Mock(return_value=response))
    manage = Mock()
    monkeypatch.setattr(module, "_manage_instance", manage)
    monkeypatch.setattr("sys.argv", ["tracknest", "stop", "--project", "same"])
    assert module.main() == 1
    output = capsys.readouterr().out
    assert expected in output
    if projects:
        assert all(project["id"] in output for project in projects)
    assert "private-token" not in output
    manage.assert_not_called()


def test_stopped_team_status_suggests_starting_the_same_project(client_module, monkeypatch, capsys):
    module = client_module
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": "http://localhost/u/alice/", "token": "secret"})
    response = Mock()
    response.json.return_value = {"projects": [{"id": "a" * 32, "name": "team-baseline", "owner": "alice"}]}
    monkeypatch.setattr(module.httpx2, "get", Mock(return_value=response))
    monkeypatch.setattr(module, "_verify_credentials", lambda *args: {"authenticated": True, "instance_running": False})
    monkeypatch.setattr(module, "_pending_status", lambda url: True)
    monkeypatch.setattr("sys.argv", ["tracknest", "status", "--project", "team-baseline", "--lang", "zh"])
    assert module.main() == 1
    assert "tracknest start --project team-baseline --lang zh" in capsys.readouterr().out


@pytest.mark.parametrize("running", [False, True])
def test_training_starts_stopped_instance_before_connecting(client_module, monkeypatch, running):
    module = client_module
    config = {"server_url": "http://localhost/u/alice/", "token": "secret"}
    monkeypatch.setattr(module, "_read_config", lambda: config)
    events = Mock()
    events.verify.return_value = {"authenticated": True, "instance_running": running}
    monkeypatch.setattr(module, "_verify_credentials", events.verify)
    monkeypatch.setattr(module, "_manage_instance", events.manage)
    monkeypatch.setattr(module, "_DirectClient", events.remote)
    monkeypatch.setattr(module._sdk, "RemoteClient", module._sdk.RemoteClient)
    run_module = module.importlib.import_module("trackio.run")
    monkeypatch.setattr(run_module, "RemoteClient", run_module.RemoteClient)
    assert module._connect() == config
    assert [call[0] for call in events.mock_calls] == (["verify", "remote"] if running else ["verify", "manage", "remote"])
    if not running:
        events.manage.assert_called_once_with(config, "start")


@pytest.mark.parametrize("failure", ["auth", "start"])
def test_training_auth_or_start_failure_prevents_sdk_initialization(client_module, monkeypatch, failure):
    module = client_module
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": "http://localhost/u/alice/", "token": "secret"})
    verify = Mock(return_value={"authenticated": True, "instance_running": False},
                  side_effect=RuntimeError("invalid token") if failure == "auth" else None)
    manage = Mock(side_effect=RuntimeError("start failed"))
    remote, init = Mock(), Mock()
    monkeypatch.setattr(module, "_verify_credentials", verify)
    monkeypatch.setattr(module, "_manage_instance", manage)
    monkeypatch.setattr(module, "_DirectClient", remote)
    monkeypatch.setattr(module._sdk, "init", init)
    with pytest.raises(RuntimeError, match="invalid token" if failure == "auth" else "start failed"):
        module.init(project="experiment")
    assert manage.call_count == (0 if failure == "auth" else 1)
    remote.assert_not_called()
    init.assert_not_called()


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
    assert "No pending uploads" not in output


@pytest.mark.parametrize("command,expected,api", [
    (["projects"], "experiment", "/get_all_projects"),
    (["runs", "experiment"], "baseline  run-1", "/get_runs_for_project"),
])
def test_cli_remote_queries_do_not_modify_proxy_environment(client_module, monkeypatch, capsys, command, expected, api):
    module = client_module
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": "http://localhost/u/alice/", "token": "secret"})
    monkeypatch.setattr(module, "_verify_credentials", lambda *args: {"authenticated": True, "instance_running": True})
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


@pytest.mark.parametrize("command", [["projects"], ["runs", "experiment"]])
@pytest.mark.parametrize("language", ["en", "zh"])
def test_queries_on_stopped_instance_explain_how_to_start_without_contacting_sdk(client_module, monkeypatch, capsys, command, language):
    module = client_module
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": "http://localhost/u/alice/", "token": "secret"})
    monkeypatch.setattr(module, "_verify_credentials", lambda *args: {"authenticated": True, "instance_running": False})
    remote, manage = Mock(), Mock()
    monkeypatch.setattr(module, "_RemoteClient", remote)
    monkeypatch.setattr(module, "_manage_instance", manage)
    monkeypatch.setattr("sys.argv", ["tracknest", *command, "--lang", language])
    assert module.main() == 1
    output = capsys.readouterr().out
    assert ("Instance is stopped" if language == "en" else "实例尚未启动") in output
    assert "tracknest start" in output
    assert "Space" not in output and "503" not in output and "developer.mozilla.org" not in output
    remote.assert_not_called()
    manage.assert_not_called()


@pytest.mark.parametrize("action,expected", [("start", "实例：运行中"), ("stop", "实例：已停止")])
@pytest.mark.parametrize("before", [False, True])
def test_chinese_language_option_works_before_and_after_subcommands(client_module, monkeypatch, capsys, action, expected, before):
    module = client_module
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": "http://localhost/u/alice/", "token": "secret"})
    monkeypatch.setattr(module, "_manage_instance", Mock())
    monkeypatch.setattr("sys.argv", ["tracknest", *(["--lang", "zh", action] if before else [action, "--lang", "zh"])])
    assert module.main() == 0
    output = capsys.readouterr().out
    assert expected in output and "Instance:" not in output


@pytest.mark.parametrize("language", ["en", "zh"])
def test_status_groups_pending_projects_and_totals_without_repeated_prefixes(client_module, monkeypatch, tmp_path, capsys, language):
    module = client_module
    config = {"server_url": "http://localhost/u/alice/", "token": "private-token"}
    monkeypatch.setattr(module, "_read_config", lambda: config)
    monkeypatch.setattr(module, "_verify_credentials", lambda *args: {"authenticated": True, "instance_running": False})
    monkeypatch.setattr(module.SQLiteStorage, "get_project_db_path", lambda project: tmp_path / f"{project}.db")
    for name, metrics, system in (("my-project-test", 100, 4), ("my-project-test1", 25, 1), ("实验项目", 100, 4)):
        with sqlite3.connect(tmp_path / f"{name}.db") as database:
            for table, count in (("metrics", metrics), ("system_metrics", system)):
                database.execute(f"CREATE TABLE {table} (space_id TEXT)")
                database.executemany(f"INSERT INTO {table} VALUES (?)", [(config["server_url"],)] * count)
    before = {path: path.read_bytes() for path in tmp_path.glob("*.db")}
    monkeypatch.setattr("sys.argv", ["tracknest", "status", "--lang", language])
    assert module.main() == 1
    output = capsys.readouterr().out
    assert output.count("tracknest:") == 1
    assert "tracknest start" in output and "private-token" not in output
    label = "Total" if language == "en" else "合计"
    assert [line.split() for line in output.splitlines() if line.strip().startswith(label)] == [[label, "225", "9", "0", "0"]]
    assert ("Pending uploads" if language == "en" else "待上传缓存") in output
    assert ("Counts exclude in-memory" if language == "en" else "统计不包含仍在训练进程内存中") in output
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("code", [403, 503])
def test_cli_explains_wrapped_sdk_http_errors(client_module, monkeypatch, capsys, language, code):
    module = client_module
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": "http://localhost/u/alice/", "token": "private-token"})
    monkeypatch.setattr(module, "_verify_credentials", lambda *args: {"authenticated": True, "instance_running": True})
    request = module.httpx2.Request("GET", "http://localhost/u/alice/version")
    response = module.httpx2.Response(code, request=request)
    wrapped = ConnectionError("Could not connect to Space. https://developer.mozilla.org/HTTP")
    wrapped.__cause__ = module.httpx2.HTTPStatusError("server error", request=request, response=response)
    monkeypatch.setattr(module, "_RemoteClient", Mock(side_effect=wrapped))
    monkeypatch.setattr("sys.argv", ["tracknest", "projects", "--lang", language])
    assert module.main() == 1
    output = capsys.readouterr().out
    expected = ("Access denied" if language == "en" else "访问被拒绝") if code == 403 else "tracknest start"
    assert expected in output
    assert "Space" not in output and "developer.mozilla.org" not in output and "private-token" not in output


@pytest.mark.parametrize("language", ["en", "zh"])
def test_cli_localizes_network_errors_with_low_level_causes(client_module, monkeypatch, capsys, language):
    module = client_module
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": "http://localhost/u/alice/", "token": "secret"})
    error = module.httpx2.ConnectError("connection refused")
    error.__cause__ = RuntimeError("low-level socket error")
    monkeypatch.setattr(module, "_verify_credentials", Mock(side_effect=error))
    remote = Mock()
    monkeypatch.setattr(module, "_RemoteClient", remote)
    monkeypatch.setattr("sys.argv", ["tracknest", "projects", "--lang", language])
    assert module.main() == 1
    output = capsys.readouterr().out
    assert ("Cannot reach TrackNest" if language == "en" else "无法连接 TrackNest") in output
    assert "socket error" not in output and "connection refused" not in output
    remote.assert_not_called()


def test_chinese_login_saves_config_without_printing_token(client_module, monkeypatch, capsys):
    module = client_module
    monkeypatch.setattr(module, "_verify_credentials", lambda *args: {"authenticated": True, "instance_running": False})
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt: "private-token")
    monkeypatch.setattr("sys.argv", ["tracknest", "login", "--server-url", "http://localhost/u/alice/", "--lang", "zh"])
    assert module.main() == 0
    output = capsys.readouterr().out
    assert "登录成功" in output and "实例：已停止" in output and "private-token" not in output
    assert json.loads(module._config_path().read_text())["token"] == "private-token"


@pytest.mark.parametrize("args", [["--lang", "zh", "--help"], ["status", "--lang", "zh", "--help"]])
def test_chinese_help_does_not_require_login(client_module, monkeypatch, capsys, args):
    monkeypatch.setattr("sys.argv", ["tracknest", *args])
    with pytest.raises(SystemExit) as result:
        client_module.main()
    assert result.value.code == 0
    assert "输出语言" in capsys.readouterr().out


@pytest.mark.parametrize("args,chinese", [
    (["runs"], False),
    (["runs", "--lang", "zh"], True),
    (["--lang", "zh", "runs"], True),
])
def test_runs_without_project_shows_actionable_guidance(client_module, monkeypatch, capsys, args, chinese):
    config = Mock(side_effect=AssertionError("Missing arguments must not read connection settings"))
    monkeypatch.setattr(client_module, "_read_config", config)
    monkeypatch.setattr("sys.argv", ["tracknest", *args])
    assert client_module.main() == 2
    captured = capsys.readouterr()
    assert ("请指定项目名称" if chinese else "Specify a project name") in captured.out
    assert "tracknest runs my-project" in captured.out
    assert "tracknest projects" in captured.out
    assert "usage:" not in captured.out and not captured.err
    config.assert_not_called()


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
@pytest.mark.parametrize("running,english,chinese,color", [
    (True, "running", "运行中", "32"),
    (False, "stopped", "已停止", "31"),
    (None, "unknown", "未知", "31"),
])
@pytest.mark.parametrize("language", ["en", "zh"])
def test_instance_status_colors(client_module, monkeypatch, capsys, tty, no_color, colored,
                                running, english, chinese, color, language):
    module = client_module
    monkeypatch.setattr(module.sys.stdout, "isatty", lambda: tty)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": "http://localhost/u/alice/", "token": "secret"})
    monkeypatch.setattr(module, "_verify_credentials", Mock(
        return_value={"authenticated": True, "instance_running": running},
        side_effect=ConnectionError("unreachable") if running is None else None))
    monkeypatch.setattr(module, "_pending_status", lambda server_url: True)
    monkeypatch.setattr("sys.argv", ["tracknest", "status", "--lang", language])
    assert module.main() == (0 if running else 1)
    output = capsys.readouterr().out
    label, value = ("Instance", english) if language == "en" else ("实例", chinese)
    line = next(line for line in output.splitlines() if re.sub(r"\033\[[0-9;]*m", "", line).strip().startswith(label + ":"))
    assert line.endswith(f"\033[{color}m{value}\033[0m" if colored else value)
    assert re.sub(r"\033\[[0-9;]*m", "", line) == f"  {label}:{' ' * (8 if language == 'en' else 6)}{value}"
    authentication = ("verified" if running is not None else "unavailable") if language == "en" else ("已验证" if running is not None else "未能验证")
    authentication_color = "32" if running is not None else "31"
    assert (f"\033[{authentication_color}m{authentication}\033[0m" if colored else authentication) in output
    if not colored:
        assert "\033[" not in output


@pytest.mark.parametrize("tty,no_color,colored", [(True, False, True), (False, False, False), (True, True, False)])
@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("command,expected", [
    (["status"], 0), (["projects"], 0), (["runs", "experiment"], 0), (["runs"], 2),
    (["start"], 0), (["stop"], 0), (["login", "--server-url", "http://localhost/u/alice/"], 0),
    (["--help"], 0), (["runs", "--help"], 0), (["--version"], 0), (["--unknown"], 2),
])
def test_cli_colors_all_text_without_changing_layout(client_module, monkeypatch, tmp_path, capsys,
                                                     tty, no_color, colored, language, command, expected):
    module = client_module
    monkeypatch.setattr(module.sys.stdout, "isatty", lambda: tty)
    monkeypatch.setattr(module.sys.stderr, "isatty", lambda: tty)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)
    url = "http://localhost/u/alice/"
    monkeypatch.setattr(module, "_read_config", lambda: {"server_url": url, "token": "private-token"})
    monkeypatch.setattr(module, "_verify_credentials", lambda *args: {"authenticated": True, "instance_running": True})
    monkeypatch.setattr(module, "_manage_instance", Mock())
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt: "private-token")
    remote = Mock()
    remote.predict.return_value = ["experiment"] if command[0] == "projects" else [
        {"name": "baseline", "id": "run-1", "created_at": "2026-11-07T12:00:00Z"}]
    monkeypatch.setattr(module, "_RemoteClient", lambda *args, **kwargs: remote)
    monkeypatch.setattr(module.SQLiteStorage, "get_project_db_path", lambda project: tmp_path / f"{project}.db")
    with sqlite3.connect(tmp_path / "experiment.db") as database:
        database.execute("CREATE TABLE metrics (space_id TEXT)")
        database.execute("INSERT INTO metrics VALUES (?)", (url,))
    monkeypatch.setattr("sys.argv", ["tracknest", "--lang", language, *command])
    try:
        code = module.main()
    except SystemExit as result:
        code = result.code
    assert code == expected
    captured = capsys.readouterr()
    for output in (captured.out, captured.err):
        assert "private-token" not in output
        if colored:
            active = False
            for part in re.split(r"(\033\[[0-9;]*m)", output):
                if part.startswith("\033["):
                    active = part != "\033[0m"
                elif part.strip():
                    assert active, f"Uncolored text: {part!r}"
        else:
            assert "\033[" not in output
    plain = re.sub(r"\033\[[0-9;]*m", "", captured.out)
    assert plain.startswith("\n") and plain.endswith("\n\n" if not captured.err else "\n")


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
