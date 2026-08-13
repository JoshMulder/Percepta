"""Serving the built console: deep links, and the two things that must NOT be
treated as app routes.

This exists because the fallback was assumed rather than tested. The mount was
built with `html=True` and a comment saying that made unknown paths fall back to
index.html - it does not; it serves index.html for a *directory* request and
answers a missing path with 404. Nothing noticed until the login page grew links
to /privacy and /terms, which a signed-out browser reaches by full navigation,
and both 404'd in production.
"""

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from backend.main import ConsoleFiles


@pytest.fixture()
def console(tmp_path: Path) -> TestClient:
    """A stand-in for the built console: an index and one hashed asset."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>Percepta</title>")
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "logo.svg").write_text("<svg/>")

    app = Starlette()
    app.mount("/", ConsoleFiles(directory=tmp_path, html=True), name="console")
    return TestClient(app)


def test_serves_the_app_at_the_root(console: TestClient) -> None:
    response = console.get("/")
    assert response.status_code == 200
    assert "Percepta" in response.text


@pytest.mark.parametrize("path", ["/privacy", "/terms", "/stations/abc/settings"])
def test_deep_links_serve_the_app(console: TestClient, path: str) -> None:
    """The reason this file exists. A link into the app from outside it - an
    email, a bookmark, the login page's own footer - has to load the app."""
    response = console.get(path)
    assert response.status_code == 200
    assert "Percepta" in response.text


def test_a_missing_asset_stays_a_404(console: TestClient) -> None:
    """A half-deployed build must fail loudly. Answering a stale asset request
    with HTML turns a deploy race into a syntax error somewhere unrelated."""
    assert console.get("/assets/index-gone.js").status_code == 404


def test_a_mistyped_api_path_stays_a_404(console: TestClient) -> None:
    """This mount catches everything the routers did not. An API path that
    answered 200 with an HTML page is the shape of bug that makes a client
    retry forever - and one this deployment has been bitten by before."""
    for path in ("/api/nonexistent", "/ws/nope", "/broker/nope", "/media/nope"):
        assert console.get(path).status_code == 404, path


def test_the_app_shell_is_never_cached(console: TestClient) -> None:
    """index.html names which fingerprinted assets to load, so a cached copy
    pins the browser to the previous build. That has to hold on the fallback
    path too, not just at the root."""
    for path in ("/", "/privacy"):
        assert console.get(path).headers["cache-control"] == "no-cache, must-revalidate", path


def test_hashed_assets_are_cached_forever(console: TestClient) -> None:
    response = console.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_unhashed_root_files_revalidate(console: TestClient) -> None:
    response = console.get("/logo.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, must-revalidate"
