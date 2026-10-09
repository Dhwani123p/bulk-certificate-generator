"""Request-level validation (422) and recipient-level validation (marked invalid)."""

import pytest

from app.config import settings
from app.validation import validate_recipients
from tests.conftest import make_payload

# ------------------------------------------------------------ request-level -> 422


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"certificate": {"course_name": "X", "issuer_name": "Y"}},  # no recipients
        make_payload([]),  # empty list
        make_payload("not-a-list"),
        make_payload([{"name": "A"}], course_name=""),
        make_payload([{"name": "A"}], course_name="   "),
        make_payload([{"name": "A"}], issuer_name="x" * 101),
        make_payload([{"name": "A"}], issue_date="not-a-date"),
        make_payload([{"name": "A"}], title="Line\nbreak"),
        {**make_payload([{"name": "A"}]), "unexpected": True},
    ],
)
def test_invalid_requests_are_rejected(client, payload):
    resp = client.post("/api/v1/jobs", json=payload)
    assert resp.status_code == 422


def test_recipient_limit_is_enforced(client, monkeypatch):
    monkeypatch.setattr(settings, "max_recipients_per_job", 3)
    resp = client.post("/api/v1/jobs", json=make_payload([{"name": f"P{i}"} for i in range(4)]))
    assert resp.status_code == 422
    assert "at most 3 recipients" in resp.text


def test_certificate_info_defaults(client):
    payload = {
        "certificate": {"course_name": "Data 101", "issuer_name": "Acme"},
        "recipients": [{"name": "A"}],
    }
    job_id = client.post("/api/v1/jobs", json=payload).json()["job_id"]
    info = client.get(f"/api/v1/jobs/{job_id}").json()["certificate"]
    assert info["title"] == "Certificate of Completion"
    assert info["issue_date"]  # defaults to today


# ---------------------------------------------------- recipient-level -> invalid


def test_invalid_recipients_are_reported_and_valid_ones_still_generated(client):
    recipients = [
        {"name": "Alice", "email": "alice@example.com"},  # 0 ok
        {"name": "   "},  # 1 blank name
        {"email": "noname@example.com"},  # 2 missing name
        {"name": "Bad Email", "email": "not-an-email"},  # 3 invalid email
        "just a string",  # 4 not an object
        {"name": "Alice Again", "email": "ALICE@example.com"},  # 5 duplicate email
        {"name": "x" * 101},  # 6 too long
        {"name": "Bob", "email": ""},  # 7 ok (blank email treated as absent)
        {"name": "李明"},  # 8 not renderable by the template font
        {"name": "José Müller"},  # 9 ok (accented Latin is supported)
    ]
    resp = client.post("/api/v1/jobs", json=make_payload(recipients))
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 3
    assert body["invalid"] == 7
    errors = {item["index"]: " ".join(item["errors"]) for item in body["invalid_recipients"]}
    assert set(errors) == {1, 2, 3, 4, 5, 6, 8}
    assert "name" in errors[1]
    assert "name" in errors[2]
    assert "email" in errors[3]
    assert "JSON object" in errors[4]
    assert "duplicate of recipient at index 0" in errors[5]
    assert "100 characters" in errors[6]
    assert "not supported by the certificate font" in errors[8]

    status = client.get(f"/api/v1/jobs/{body['job_id']}").json()
    assert status["status"] == "completed_with_errors"
    assert status["progress"]["generated"] == 3
    assert status["progress"]["invalid"] == 7
    assert {f["index"] for f in status["failures"]} == {1, 2, 3, 4, 5, 6, 8}
    assert all(f["status"] == "invalid" for f in status["failures"])


def test_job_with_only_invalid_recipients_fails_immediately(client):
    resp = client.post("/api/v1/jobs", json=make_payload([{"name": ""}, {"foo": "bar"}]))
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "failed"
    assert body["accepted"] == 0


def test_validate_recipients_unit():
    valid, rejected = validate_recipients(
        [{"name": "  Padded  ", "extra_field": "ignored"}, {"name": "Tab\there"}, None]
    )
    assert [v.recipient.name for v in valid] == ["Padded"]
    assert [r.index for r in rejected] == [1, 2]
    assert "control characters" in rejected[0].errors[0]
