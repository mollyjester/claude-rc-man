"""The application as it is started in production."""

import os
import subprocess
import sys


def test_packages_import_from_the_environment_alone():
    """Not through pytest's pythonpath: from another directory, with no PYTHONPATH."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c", "import rcman.main, helpers.rules, helpers.screens"],
        cwd="/",
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_openapi_lists_the_api(client):
    paths = client.get("/api/openapi.json").json()["paths"]
    assert "/api/workspaces" in paths and "/api/people/{person}" in paths
