"""Deployment-local storage selection shared by scripts and Flask commands."""
import json
import os
from pathlib import Path
import tempfile

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / ".storage.json"


def storage_paths():
    saved = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    state = os.environ.get("MANAGER_STATE_DIR", saved.get("state_dir", BASE_DIR / "instance"))
    data = os.environ.get("MANAGER_DATA_ROOT", saved.get("data_root", BASE_DIR / "data"))
    return Path(state).expanduser().resolve(), Path(data).expanduser().resolve()


def select_storage(root=None):
    if root is not None:
        root = root.expanduser().resolve()
        state, data = root / "instance", root / "data"
    else:
        state = Path(os.environ.get("MANAGER_STATE_DIR", BASE_DIR / "instance")).expanduser().resolve()
        data = Path(os.environ.get("MANAGER_DATA_ROOT", BASE_DIR / "data")).expanduser().resolve()
    os.environ["MANAGER_STATE_DIR"] = str(state)
    os.environ["MANAGER_DATA_ROOT"] = str(data)
    fd, name = tempfile.mkstemp(dir=BASE_DIR, prefix=".storage.json.")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"state_dir": str(state), "data_root": str(data)}, handle)
        temporary.replace(CONFIG_FILE)
    finally:
        temporary.unlink(missing_ok=True)
