"""`/favicon.ico` on the api — a browser opening the JSON API must not log a 404.

Also covers the package entrypoint: `inframind.api` exposes `create_app` lazily, so
`python -m inframind.api.app` does not import the module twice.
"""

from __future__ import annotations

import subprocess
import sys

import httpx
from fakeredis import FakeAsyncRedis

from inframind.api.app import create_app


async def test_favicon_is_204_and_hidden_from_the_schema() -> None:
    app = create_app(redis=FakeAsyncRedis(), webhook_secret="s3cret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        r = await client.get("/favicon.ico")
    assert r.status_code == 204
    assert r.content == b""
    assert "/favicon.ico" not in app.openapi()["paths"]


def test_package_exposes_create_app_lazily() -> None:
    import inframind.api as api

    assert api.create_app is create_app


def test_importing_the_package_does_not_import_the_app_module() -> None:
    """Otherwise `python -m inframind.api.app` warns (runpy) and runs the module twice."""
    code = "import sys, inframind.api; print('inframind.api.app' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "False"
