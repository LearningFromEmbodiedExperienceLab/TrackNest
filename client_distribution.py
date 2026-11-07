"""Build the small, credential-free client wheel using only the standard library."""
import ast
import base64
import csv
import hashlib
from io import BytesIO, StringIO
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_DEFLATED

CLIENT_DIR = Path(__file__).resolve().parent / "client"
_project = (CLIENT_DIR / "pyproject.toml").read_text()
_version = re.search(r'^version = "([^"]+)"$', _project, re.M)[1]
CLIENT_WHEEL_NAME = f"tracknest_client-{_version}-py3-none-any.whl"


def build_client_wheel():
    dist = f"tracknest_client-{_version}.dist-info"
    requires_python = re.search(r'^requires-python = "([^"]+)"$', _project, re.M)[1]
    dependencies = ast.literal_eval(re.search(r'^dependencies = (.+)$', _project, re.M)[1])
    metadata = (f"Metadata-Version: 2.1\nName: tracknest-client\nVersion: {_version}\n"
                f"Requires-Python: {requires_python}\n"
                + "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies) + "\n")
    files = {
        "tracknest.py": (CLIENT_DIR / "tracknest.py").read_bytes(),
        f"{dist}/METADATA": metadata.encode(),
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: TrackNest\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist}/entry_points.txt": ("[console_scripts]\ntracknest = "
            + re.search(r'^tracknest = "([^"]+)"$', _project, re.M)[1] + "\n").encode(),
    }
    record = StringIO(newline="")
    writer = csv.writer(record)
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow([name, "sha256=" + digest, len(content)])
    writer.writerow([f"{dist}/RECORD", "", ""])
    files[f"{dist}/RECORD"] = record.getvalue().encode()
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as wheel:
        for name, content in files.items():
            wheel.writestr(name, content)
    return output.getvalue()
