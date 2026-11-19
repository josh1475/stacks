"""
Test setup: Stacks runs against a throwaway project root (config, queue database, downloads), and no test makes a
network request: catalogue HTTP goes through FakeSession, which answers from recorded fixtures.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT = Path(tempfile.mkdtemp(prefix="stacks-tests-"))
(ROOT / "files").mkdir()
shutil.copy(REPO / "files" / "config_schema.yaml", ROOT / "files" / "config_schema.yaml")
shutil.copy(REPO / "VERSION", ROOT / "VERSION")
os.environ["STACKS_PROJECT_ROOT"] = str(ROOT)
sys.path.insert(0, str(REPO / "src"))

FIXTURES = REPO / "tests" / "fixtures" / "catalogue"
ADMIN_KEY = "A" * 32
DOWNLOADER_KEY = "D" * 32


def fixture(name, mode="rb"):
    with open(FIXTURES / name, mode) as f:
        return f.read()


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self.body = body if isinstance(body, bytes) else body.encode()
        self.headers = dict(headers or {})
        self.closed = False

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def close(self):
        self.closed = True


class FakeSession:
    """Answers GETs from a dict: url -> FakeResponse, or a list of them (one per call), or a callable(url, headers)."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.headers = {}
        self.calls = []

    def get(self, url, headers=None, timeout=None, stream=None, allow_redirects=True):
        assert allow_redirects is False, "redirects must be followed by hand (each hop checked)"
        self.calls.append((url, dict(headers or {})))
        r = self.routes.get(url)
        if r is None:
            raise AssertionError(f"unexpected request: {url}")
        if callable(r):
            return r(url, headers or {})
        if isinstance(r, list):
            return r.pop(0)
        return r


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_http(clock):
    from stacks.catalogue.http import CatalogueHTTP

    def make(routes):
        return CatalogueHTTP(session=FakeSession(routes), clock=clock, sleep=clock.sleep, wall=lambda: 1_700_000_000.0)
    return make


@pytest.fixture
def fresh_db(monkeypatch, tmp_path):
    """An empty queue database in its own folder."""
    from stacks.coordinator import database
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "queue.db")
    database.init_database()
    return tmp_path / "queue.db"


@pytest.fixture
def config_file(tmp_path):
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"api": {"key": ADMIN_KEY, "downloader_key": DOWNLOADER_KEY},
                                 "downloads": {"delay": 0}}))
    return p


@pytest.fixture
def app(config_file, fresh_db, monkeypatch):
    monkeypatch.delenv("CATALOGUE_ONLY", raising=False)
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    from stacks.server.webserver import create_app
    a = create_app(str(config_file))
    a.testing = True
    return a
