import base64
import csv
import hashlib
from io import BytesIO, StringIO
import json
import subprocess
import sys
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
        assert b"Requires-Dist:" not in wheel.read(metadata)
        entry_points = next(name for name in wheel.namelist() if name.endswith("/entry_points.txt"))
        assert b"tracknest = tracknest:main" in wheel.read(entry_points)


def test_wheel_installs_without_changing_other_packages(ctx, tmp_path):
    environment = tmp_path / "environment"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, timeout=30)
    python = str(environment / "bin/python")

    def run(*args):
        return subprocess.run([python, *args], capture_output=True, text=True, timeout=30)

    packages = "import importlib.metadata as m, json; print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))"
    before = json.loads(run("-c", packages).stdout)
    wheel = tmp_path / CLIENT_WHEEL_NAME
    wheel.write_bytes(app.test_client().get("/client/" + CLIENT_WHEEL_NAME).data)
    installed = run("-m", "pip", "install", "--no-index", str(wheel))
    assert installed.returncode == 0, installed.stderr
    after = json.loads(run("-c", packages).stdout)
    assert after.pop("tracknest-client") == CLIENT_WHEEL_NAME.split("-")[1]
    assert after == before

    imported = run("-c", "import tracknest, sys; assert 'trackio' not in sys.modules")
    assert imported.returncode == 0, imported.stderr
    for args in (("--help",), ("--lang", "zh", "--help"), ("--version",)):
        result = run("-m", "tracknest", *args)
        assert result.returncode == 0, result.stderr
    for args, message in (((), "does not install dependencies automatically"),
                          (("--lang", "zh"), "不会自动安装依赖")):
        result = run("-m", "tracknest", *args, "status")
        assert result.returncode == 1
        assert message in result.stdout
        assert "Traceback" not in result.stderr
    training = run("-c", "import tracknest; tracknest.init(project='test')")
    assert training.returncode != 0
    assert "does not install dependencies automatically" in training.stderr


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
    assert all(command in result.stdout for command in ("status", "start", "stop", "projects", "runs"))
    result = subprocess.run([str(target / "bin/tracknest"), "--version"], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(target)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and CLIENT_WHEEL_NAME.split("-")[1] in result.stdout
    result = subprocess.run([str(target / "bin/tracknest"), "--lang", "zh", "--help"], cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(target)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and "输出语言" in result.stdout and "启动当前实例" in result.stdout
