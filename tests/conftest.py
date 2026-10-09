import os
import tempfile
from pathlib import Path

# Point the app at a throwaway database *before* app modules create the engine.
_TMP = Path(tempfile.mkdtemp(prefix="certgen-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["STORAGE_DIR"] = str(_TMP / "files")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import generator  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Fresh tables and storage dir for every test; jobs processed inline by default."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "certificates")
    monkeypatch.setattr(settings, "sync_processing", True)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fail_for(monkeypatch):
    """Make certificate rendering raise for the given recipient names."""

    def _install(*names: str):
        real_render = generator.render_certificate

        def flaky_render(data, path):
            if data.recipient_name in names:
                raise RuntimeError(f"simulated rendering failure for {data.recipient_name}")
            return real_render(data, path)

        monkeypatch.setattr(generator, "render_certificate", flaky_render)
        return real_render

    return _install


def make_payload(recipients, **certificate):
    info = {
        "title": "Certificate of Completion",
        "course_name": "Intro to Python",
        "issuer_name": "Acme Academy",
        "issue_date": "2026-10-01",
    }
    info.update(certificate)
    return {"certificate": info, "recipients": recipients}
