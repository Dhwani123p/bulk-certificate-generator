"""Certificate rendering and retrieval endpoints."""

import io
import zipfile
from datetime import date
from pathlib import Path

from pypdf import PdfReader

from app.database import SessionLocal
from app.generator import CertificateData, render_certificate
from app.models import Certificate
from app.worker import runner
from tests.conftest import make_payload


def _pdf_text(data: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def _create_job(client, recipients):
    return client.post("/api/v1/jobs", json=make_payload(recipients)).json()["job_id"]


# ------------------------------------------------------------------- generation


def test_render_certificate_contains_recipient_details(tmp_path):
    out = render_certificate(
        CertificateData(
            certificate_id="abc-123",
            recipient_name="Grace Hopper",
            title="Certificate of Achievement",
            course_name="Compilers",
            issuer_name="Navy Academy",
            issue_date=date(2026, 10, 1),
        ),
        tmp_path / "nested" / "cert.pdf",
    )

    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    text = _pdf_text(data)
    for expected in (
        "Grace Hopper",
        "CERTIFICATE OF ACHIEVEMENT",
        "Compilers",
        "Navy Academy",
        "01 October 2026",
        "abc-123",
    ):
        assert expected in text
    assert not list(out.parent.glob("*.tmp"))  # atomic write leaves no temp file


def test_render_handles_very_long_names(tmp_path):
    long_name = "Maximiliana " * 8
    out = render_certificate(
        CertificateData("id", long_name.strip(), "T", "C", "I", date(2026, 1, 1)),
        tmp_path / "long.pdf",
    )
    assert "Maximiliana" in _pdf_text(out.read_bytes())


# -------------------------------------------------------------------- retrieval


def test_list_certificates_with_pagination_and_filter(client):
    job_id = _create_job(client, [{"name": f"Person {i}"} for i in range(5)] + [{"name": ""}])

    page = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"limit": 2, "offset": 1}).json()
    assert page["total"] == 6
    assert [c["index"] for c in page["items"]] == [1, 2]

    invalid = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"status": "invalid"}).json()
    assert invalid["total"] == 1
    assert invalid["items"][0]["download_url"] is None
    assert invalid["items"][0]["error"]

    generated = client.get(
        f"/api/v1/jobs/{job_id}/certificates", params={"status": "generated"}
    ).json()
    assert generated["total"] == 5
    assert all(c["download_url"] for c in generated["items"])


def test_list_certificates_rejects_unknown_status(client):
    job_id = _create_job(client, [{"name": "A"}])
    resp = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"status": "bogus"})
    assert resp.status_code == 422


def test_download_single_certificate(client):
    job_id = _create_job(client, [{"name": "Ada Lovelace", "email": "ada@example.com"}])
    cert = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"][0]

    details = client.get(f"/api/v1/certificates/{cert['certificate_id']}").json()
    assert details["recipient_email"] == "ada@example.com"
    assert details["status"] == "generated"

    resp = client.get(cert["download_url"])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "certificate-ada-lovelace" in resp.headers["content-disposition"]
    text = _pdf_text(resp.content)
    assert "Ada Lovelace" in text
    assert "Intro to Python" in text


def test_download_failed_certificate_is_conflict(client, fail_for):
    fail_for("Broken")
    job_id = _create_job(client, [{"name": "Fine"}, {"name": "Broken"}])
    failed = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"status": "failed"}).json()
    cert_id = failed["items"][0]["certificate_id"]

    resp = client.get(f"/api/v1/certificates/{cert_id}/download")
    assert resp.status_code == 409
    assert "failed" in resp.json()["detail"]


def test_download_unknown_certificate_is_404(client):
    assert client.get("/api/v1/certificates/nope/download").status_code == 404
    assert client.get("/api/v1/certificates/nope").status_code == 404


def test_download_missing_file_is_gone(client):
    job_id = _create_job(client, [{"name": "Vanishing"}])
    cert = client.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"][0]
    with SessionLocal() as db:
        Path(db.get(Certificate, cert["certificate_id"]).file_path).unlink()

    assert client.get(cert["download_url"]).status_code == 410


def test_download_job_zip_contains_only_generated_certificates(client, fail_for):
    fail_for("Bob")
    job_id = _create_job(client, [{"name": "Alice"}, {"name": "Bob"}, {"name": "Carol"}])

    resp = client.get(f"/api/v1/jobs/{job_id}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        names = sorted(archive.namelist())
        assert names == ["00001-alice.pdf", "00003-carol.pdf"]
        assert "Carol" in _pdf_text(archive.read("00003-carol.pdf"))


def test_download_job_zip_while_in_progress_is_conflict(client, monkeypatch):
    monkeypatch.setattr(runner, "submit", lambda job_id: None)
    job_id = _create_job(client, [{"name": "Alice"}])
    assert client.get(f"/api/v1/jobs/{job_id}/download").status_code == 409
