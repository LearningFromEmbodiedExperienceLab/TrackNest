import os
import tempfile
from pathlib import Path

import pytest

_state = tempfile.TemporaryDirectory(prefix="trackio-manager-tests-")
os.environ["MANAGER_STATE_DIR"] = _state.name
os.environ["MANAGER_DATA_ROOT"] = str(Path(_state.name) / "data")

from app import app, create_user, db, init_db


@pytest.fixture
def ctx():
    app.config["TESTING"] = True
    with app.app_context():
        db.drop_all()
        init_db()
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture
def users(ctx):
    return {
        name: create_user(name, "test-password-123", admin=name == "admin")
        for name in ("admin", "alice", "bob")
    }


def login(client, username):
    client.get("/login")
    response = post(client, "/login", username=username, password="test-password-123")
    if response.status_code == 302:
        client.get("/")
    return response


def post(client, path, **data):
    with client.session_transaction() as session:
        data["csrf_token"] = session["csrf_token"]
    return client.post(path, data=data)
