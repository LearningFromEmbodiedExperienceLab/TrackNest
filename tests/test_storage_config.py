import json

import pytest

import storage_config as storage


@pytest.fixture(autouse=True)
def local_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    monkeypatch.setattr(storage, "CONFIG_FILE", tmp_path / ".storage.json")
    monkeypatch.delenv("MANAGER_STATE_DIR", raising=False)
    monkeypatch.delenv("MANAGER_DATA_ROOT", raising=False)


def test_storage_selection_is_remembered_for_commands_but_start_defaults_are_unchanged(tmp_path, monkeypatch):
    defaults = (tmp_path / "instance", tmp_path / "data")
    assert storage.storage_paths() == defaults
    selected = tmp_path / "selected storage"
    storage.select_storage(selected)
    expected = (selected / "instance", selected / "data")
    assert storage.storage_paths() == expected
    assert storage.CONFIG_FILE.stat().st_mode & 0o777 == 0o600
    monkeypatch.delenv("MANAGER_STATE_DIR")
    monkeypatch.delenv("MANAGER_DATA_ROOT")
    assert storage.storage_paths() == expected
    storage.select_storage()
    assert storage.storage_paths() == defaults
    assert json.loads(storage.CONFIG_FILE.read_text())["data_root"] == str(defaults[1])


def test_explicit_storage_option_overrides_environment_and_environment_can_override_saved_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("MANAGER_STATE_DIR", str(tmp_path / "environment state"))
    monkeypatch.setenv("MANAGER_DATA_ROOT", str(tmp_path / "environment data"))
    storage.select_storage()
    assert storage.storage_paths() == (tmp_path / "environment state", tmp_path / "environment data")
    selected = tmp_path / "chosen"
    storage.select_storage(selected)
    assert storage.storage_paths() == (selected / "instance", selected / "data")
    monkeypatch.setenv("MANAGER_DATA_ROOT", str(tmp_path / "other data"))
    assert storage.storage_paths()[1] == tmp_path / "other data"
