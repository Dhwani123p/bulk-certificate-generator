import enum
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, enum.Enum):
    PENDING = "pending"  # accepted, waiting for a worker
    PROCESSING = "processing"  # a worker is generating certificates
    COMPLETED = "completed"  # every recipient got a certificate
    COMPLETED_WITH_ERRORS = "completed_with_errors"  # some succeeded, some failed/invalid
    FAILED = "failed"  # nothing was generated

    @property
    def is_finished(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_ERRORS, JobStatus.FAILED}


class CertificateStatus(str, enum.Enum):
    PENDING = "pending"  # valid, not generated yet
    GENERATED = "generated"  # PDF written to storage
    FAILED = "failed"  # generation raised an error (retryable)
    INVALID = "invalid"  # recipient data failed validation (never generated)


def _enum_column(enum_cls: type[enum.Enum]) -> Enum:
    # Store the enum *value* ("pending") as a VARCHAR; portable across SQLite/Postgres.
    return Enum(
        enum_cls,
        native_enum=False,
        values_callable=lambda members: [m.value for m in members],
        length=32,
        validate_strings=True,
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    status: Mapped[JobStatus] = mapped_column(
        _enum_column(JobStatus), default=JobStatus.PENDING, index=True
    )

    # Shared certificate information (same for every recipient in the job).
    title: Mapped[str] = mapped_column(String(100))
    course_name: Mapped[str] = mapped_column(String(200))
    issuer_name: Mapped[str] = mapped_column(String(100))
    issue_date: Mapped[date] = mapped_column(Date)

    total_count: Mapped[int] = mapped_column(Integer)
    # Job-level error (e.g. the worker crashed), distinct from per-certificate errors.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="raise"
    )


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (Index("ix_certificates_job_status", "job_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    # Position of the recipient in the submitted list, so clients can map results back.
    row_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[CertificateStatus] = mapped_column(
        _enum_column(CertificateStatus), default=CertificateStatus.PENDING
    )

    recipient_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Original submitted entry; kept for invalid rows so the client can see what was rejected.
    raw_data: Mapped[Any] = mapped_column(JSON, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[Job] = relationship(back_populates="certificates", lazy="raise")
