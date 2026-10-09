"""Request / response models.

Validation happens at two levels:

* Request level (``JobCreate``) - the shape of the request itself: certificate
  info must be valid and ``recipients`` must be a non-empty list within the size
  limit. Failures here reject the whole request with HTTP 422, because there is
  nothing sensible to generate.
* Recipient level (``RecipientIn``) - each recipient is validated individually
  in ``app.validation``. A bad recipient is recorded as ``invalid`` on the job
  instead of rejecting the request, so one typo doesn't block hundreds of valid
  certificates.
"""

import re
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from app.config import settings
from app.models import CertificateStatus, JobStatus

# Rejects control characters (newlines, tabs, NUL...) that would break the layout.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _printable_text(value: str) -> str:
    if _CONTROL_CHARS.search(value):
        raise ValueError("must not contain control characters")
    # The template uses ReportLab's built-in fonts, which only cover cp1252
    # (Latin alphabets). Reject anything else rather than silently printing
    # boxes on a certificate. Embedding a Unicode TTF font would lift this.
    try:
        value.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"contains characters not supported by the certificate font: {value[exc.start:exc.end]!r}"
        ) from None
    return value


TrimmedStr = Annotated[str, StringConstraints(strip_whitespace=True)]


# --------------------------------------------------------------------------- requests


class CertificateInfo(BaseModel):
    """Information shared by every certificate in the job."""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[TrimmedStr, Field(min_length=1, max_length=100)] = (
        "Certificate of Completion"
    )
    course_name: Annotated[TrimmedStr, Field(min_length=1, max_length=200)]
    issuer_name: Annotated[TrimmedStr, Field(min_length=1, max_length=100)]
    issue_date: date = Field(default_factory=date.today)

    _check_chars = field_validator("title", "course_name", "issuer_name")(_printable_text)


class RecipientIn(BaseModel):
    """A single recipient. Validated per entry, see module docstring."""

    model_config = ConfigDict(extra="ignore")

    name: Annotated[TrimmedStr, Field(min_length=1, max_length=100)]
    email: EmailStr | None = None

    _check_chars = field_validator("name")(_printable_text)

    @field_validator("email", mode="before")
    @classmethod
    def _blank_email_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    certificate: CertificateInfo
    # Typed as ``Any`` on purpose: entries are validated one by one so that an
    # invalid entry is reported per recipient instead of failing the request.
    recipients: list[Any] = Field(min_length=1)

    @field_validator("recipients")
    @classmethod
    def _check_size(cls, value: list[Any]) -> list[Any]:
        limit = settings.max_recipients_per_job
        if len(value) > limit:
            raise ValueError(f"a job may contain at most {limit} recipients (got {len(value)})")
        return value


# -------------------------------------------------------------------------- responses


class InvalidRecipient(BaseModel):
    index: int
    errors: list[str]


class JobCreated(BaseModel):
    job_id: str
    status: JobStatus
    total: int
    accepted: int
    invalid: int
    invalid_recipients: list[InvalidRecipient]
    status_url: str
    certificates_url: str


class Progress(BaseModel):
    total: int
    pending: int
    generated: int
    failed: int
    invalid: int
    processed: int
    percent: float


class FailureItem(BaseModel):
    certificate_id: str
    index: int
    recipient_name: str | None
    status: CertificateStatus
    error: str | None


class JobStatusOut(BaseModel):
    job_id: str
    status: JobStatus
    certificate: CertificateInfo
    progress: Progress
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failures: list[FailureItem]
    failures_truncated: bool
    certificates_url: str
    download_url: str | None


class CertificateOut(BaseModel):
    certificate_id: str
    job_id: str
    index: int
    status: CertificateStatus
    recipient_name: str | None
    recipient_email: str | None
    error: str | None
    generated_at: datetime | None
    download_url: str | None


class CertificatePage(BaseModel):
    items: list[CertificateOut]
    total: int
    limit: int
    offset: int
