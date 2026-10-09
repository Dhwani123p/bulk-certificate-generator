"""Job creation, status/progress, failure handling and retry."""

import time
from datetime import date

from app import generator
from app.config import settings
from app.database import SessionLocal
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.worker import process_job, resume_unfinished_jobs, runner
from tests.conftest import make_payload

RECIPIENTS = [
    {"name": "Alice Smith", "email": "alice@example.com"},
    {"name": "Bob Jones", "email": "bob@example.com"},
    {"name": "Chandra Patel"},
]


def test_create_job_generates_all_certificates(client):
    resp = client.post("/api/v1/jobs", json=make_payload(RECIPIENTS))

    assert resp.status_code == 202
    body = resp.json()
    assert body["total"] == 3
    assert body["accepted"] == 3
    assert body["invalid"] == 0
    assert body["status_url"].endswith(f"/api/v1/jobs/{body['job_id']}")

    status = client.get(f"/api/v1/jobs/{body['job_id']}").json()
    assert status["status"] == "completed"
    assert status["progress"] == {
        "total": 3,
        "pending": 0,
        "generated": 3,
        "failed": 0,
        "invalid": 0,
        "processed": 3,
        "percent": 100.0,
    }
    assert status["failures"] == []
    assert status["certificate"]["course_name"] == "Intro to Python"
    assert status["started_at"] and status["completed_at"]
    assert status["download_url"]


def test_unknown_job_returns_404(client):
    assert client.get("/api/v1/jobs/does-not-exist").status_code == 404


def test_progress_while_job_is_pending(client, monkeypatch):
    # Accept the job without processing it, to observe the intermediate state.
    submitted = []
    monkeypatch.setattr(runner, "submit", submitted.append)

    job_id = client.post("/api/v1/jobs", json=make_payload(RECIPIENTS)).json()["job_id"]
    assert submitted == [job_id]

    status = client.get(f"/api/v1/jobs/{job_id}").json()
    assert status["status"] == "pending"
    assert status["progress"]["pending"] == 3
    assert status["progress"]["percent"] == 0.0
    assert status["download_url"] is None

    process_job(job_id)

    status = client.get(f"/api/v1/jobs/{job_id}").json()
    assert status["status"] == "completed"
    assert status["progress"]["generated"] == 3


def test_progress_counts_invalid_recipients_as_processed(client, monkeypatch):
    monkeypatch.setattr(runner, "submit", lambda job_id: None)
    payload = make_payload([{"name": "Valid"}, {"name": ""}, {"name": "Also Valid"}, "bad"])
    job_id = client.post("/api/v1/jobs", json=payload).json()["job_id"]

    progress = client.get(f"/api/v1/jobs/{job_id}").json()["progress"]
    assert progress["total"] == 4
    assert progress["invalid"] == 2
    assert progress["pending"] == 2
    assert progress["processed"] == 2
    assert progress["percent"] == 50.0


def test_single_failure_does_not_block_other_certificates(client, fail_for):
    fail_for("Bob Jones")

    job_id = client.post("/api/v1/jobs", json=make_payload(RECIPIENTS)).json()["job_id"]
    status = client.get(f"/api/v1/jobs/{job_id}").json()

    assert status["status"] == "completed_with_errors"
    assert status["progress"]["generated"] == 2
    assert status["progress"]["failed"] == 1
    assert len(status["failures"]) == 1
    failure = status["failures"][0]
    assert failure["index"] == 1
    assert failure["recipient_name"] == "Bob Jones"
    assert failure["status"] == "failed"
    assert "simulated rendering failure" in failure["error"]

    generated = client.get(
        f"/api/v1/jobs/{job_id}/certificates", params={"status": "generated"}
    ).json()
    assert [c["recipient_name"] for c in generated["items"]] == ["Alice Smith", "Chandra Patel"]


def test_job_fails_when_every_generation_fails(client, fail_for):
    fail_for("Alice Smith", "Bob Jones", "Chandra Patel")
    job_id = client.post("/api/v1/jobs", json=make_payload(RECIPIENTS)).json()["job_id"]

    status = client.get(f"/api/v1/jobs/{job_id}").json()
    assert status["status"] == "failed"
    assert status["progress"]["failed"] == 3
    assert status["download_url"] is None


def test_retry_regenerates_failed_certificates(client, fail_for, monkeypatch):
    real_render = fail_for("Bob Jones")
    job_id = client.post("/api/v1/jobs", json=make_payload(RECIPIENTS)).json()["job_id"]
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "completed_with_errors"

    # The transient problem goes away; retry only touches the failed certificate.
    monkeypatch.setattr(generator, "render_certificate", real_render)
    resp = client.post(f"/api/v1/jobs/{job_id}/retry")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "completed"
    assert body["progress"]["generated"] == 3
    assert body["failures"] == []

    with SessionLocal() as db:
        bob = db.query(Certificate).filter_by(job_id=job_id, recipient_name="Bob Jones").one()
        assert bob.attempts == 2


def test_retry_rejected_when_nothing_failed(client):
    job_id = client.post("/api/v1/jobs", json=make_payload(RECIPIENTS)).json()["job_id"]
    assert client.post(f"/api/v1/jobs/{job_id}/retry").status_code == 409


def test_background_processing_mode(client, monkeypatch):
    """With the real thread pool the request returns immediately; clients poll."""
    monkeypatch.setattr(settings, "sync_processing", False)
    recipients = [{"name": f"Participant {i}"} for i in range(25)]

    resp = client.post("/api/v1/jobs", json=make_payload(recipients))
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    deadline = time.time() + 30
    while time.time() < deadline:
        status = client.get(f"/api/v1/jobs/{job_id}").json()
        if status["status"] not in ("pending", "processing"):
            break
        time.sleep(0.1)

    assert status["status"] == "completed"
    assert status["progress"]["generated"] == 25


def test_unfinished_jobs_are_resumed_on_startup(client):
    # Simulate a crash mid-job: one certificate done, one still pending.
    with SessionLocal() as db:
        job = Job(
            title="Certificate of Completion",
            course_name="Crash Recovery 101",
            issuer_name="Acme",
            issue_date=date(2026, 10, 1),
            total_count=2,
            status=JobStatus.PROCESSING,
        )
        db.add(job)
        db.flush()
        db.add(
            Certificate(
                job_id=job.id,
                row_index=0,
                recipient_name="Done Already",
                status=CertificateStatus.GENERATED,
                attempts=1,
            )
        )
        db.add(Certificate(job_id=job.id, row_index=1, recipient_name="Still Pending"))
        db.commit()
        job_id = job.id

    assert resume_unfinished_jobs() == [job_id]

    status = client.get(f"/api/v1/jobs/{job_id}").json()
    assert status["status"] == "completed"
    with SessionLocal() as db:
        done = db.query(Certificate).filter_by(job_id=job_id, row_index=0).one()
        assert done.attempts == 1  # already-generated certificates are not regenerated
