import base64
import csv
import hashlib
from io import BytesIO, StringIO
from zipfile import ZipFile

from app import app, BASE_DIR
from client_distribution import CLIENT_WHEEL_NAME


def test_public_wheel_contains_only_client_code_and_valid_metadata(ctx):
    response = app.test_client().get("/client/" + CLIENT_WHEEL_NAME)
    assert response.status_code == 200
    assert CLIENT_WHEEL_NAME in response.headers["Content-Disposition"]
    with ZipFile(BytesIO(response.data)) as wheel:
        assert len(wheel.namelist()) == 5
        assert wheel.read("tracknest.py") == (BASE_DIR / "client/tracknest.py").read_bytes()
        record = next(name for name in wheel.namelist() if name.endswith("/RECORD"))
        for name, digest, size in csv.reader(StringIO(wheel.read(record).decode())):
            if name == record:
                continue
            content = wheel.read(name)
            assert int(size) == len(content)
            assert digest == "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        metadata = next(name for name in wheel.namelist() if name.endswith("/METADATA"))
        assert b"Requires-Dist: trackio==0.45.0" in wheel.read(metadata)
        entry_points = next(name for name in wheel.namelist() if name.endswith("/entry_points.txt"))
        assert b"tracknest = tracknest:main" in wheel.read(entry_points)


def check_wheel_install(public, tmp_path):
    """Download and install from real Nginx without a login or repository checkout."""
    import os
    import subprocess
    import sys
    import requests

    session = requests.Session()
    session.trust_env = False
    response = session.get(public + "/client/" + CLIENT_WHEEL_NAME, timeout=10)
    assert response.status_code == 200
    wheel = tmp_path / CLIENT_WHEEL_NAME
    wheel.write_bytes(response.content)
    target = tmp_path / "installed-client"
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-deps",
                             "--target", str(target), str(wheel)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    code = "import tracknest; print(tracknest.__file__); tracknest._status('Installed without a repository checkout', 'success')"
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(target)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert str(target / "tracknest.py") in result.stdout
    assert "Installed without a repository checkout" in result.stdout
    assert "\033[" not in result.stdout
    result = subprocess.run([str(target / "bin/tracknest"), "--help"], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(target)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "status" in result.stdout and "projects" in result.stdout and "runs" in result.stdout
