import os
import re
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.database import get_db
from app.models import Certificate, CertificateStatus, Job, JobStatus
from app.schemas import (
    CertificateInfo,
    CertificateOut,
    CertificatePage,
    FailureItem,
    InvalidRecipient,
    JobCreate,
    JobCreated,
    JobStatusOut,
    Progress,
)
from app.validation import validate_recipients
from app.worker import finalize_job, reset_failed_certificates, runner, status_counts

router = APIRouter(prefix="/api/v1")

MAX_FAILURES_IN_STATUS = 100


# ----------------------------------------------------------------------------- helpers


def _get_job(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Job {job_id} not found")
    return job


def _raw_name(raw: object) -> str | None:
    """Best-effort name of a rejected entry, so failures are recognisable in listings."""
    if isinstance(raw, dict) and isinstance(raw.get("name"), str):
        return raw["name"].strip()[:100] or None
    return None


def _slug(text: str | None) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return slug[:60] or "certificate"


def _certificate_download_url(request: Request, cert: Certificate) -> str | None:
    if cert.status != CertificateStatus.GENERATED:
        return None
    return str(request.url_for("download_certificate", certificate_id=cert.id))


def _certificate_out(request: Request, cert: Certificate) -> CertificateOut:
    return CertificateOut(
        certificate_id=cert.id,
        job_id=cert.job_id,
        index=cert.row_index,
        status=cert.status,
        recipient_name=cert.recipient_name,
        recipient_email=cert.recipient_email,
        error=cert.error,
        generated_at=cert.generated_at,
        download_url=_certificate_download_url(request, cert),
    )


def _progress(counts: dict[CertificateStatus, int]) -> Progress:
    total = sum(counts.values())
    processed = total - counts[CertificateStatus.PENDING]
    return Progress(
        total=total,
        pending=counts[CertificateStatus.PENDING],
        generated=counts[CertificateStatus.GENERATED],
        failed=counts[CertificateStatus.FAILED],
        invalid=counts[CertificateStatus.INVALID],
        processed=processed,
        percent=round(processed / total * 100, 2) if total else 100.0,
    )


# ------------------------------------------------------------------------------ jobs


@router.post(
    "/jobs",
    response_model=JobCreated,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a bulk certificate generation job",
)
def create_job(payload: JobCreate, request: Request, db: Session = Depends(get_db)) -> JobCreated:
    valid, rejected = validate_recipients(payload.recipients)
    info = payload.certificate

    job = Job(
        title=info.title,
        course_name=info.course_name,
        issuer_name=info.issuer_name,
        issue_date=info.issue_date,
        total_count=len(payload.recipients),
    )
    db.add(job)
    db.flush()  # assigns job.id

    rows = [
        Certificate(
            job_id=job.id,
            row_index=v.index,
            recipient_name=v.recipient.name,
            recipient_email=v.recipient.email,
        )
        for v in valid
    ] + [
        Certificate(
            job_id=job.id,
            row_index=r.index,
            status=CertificateStatus.INVALID,
            recipient_name=_raw_name(r.raw),
            raw_data=r.raw,
            error="; ".join(r.errors),
        )
        for r in rejected
    ]
    db.add_all(rows)
    db.commit()

    if valid:
        # Commit first, then dispatch: the worker reads the job from the DB.
        runner.submit(job.id)
    else:
        # Nothing to generate - close the job straight away as failed.
        finalize_job(db, job)

    db.refresh(job)
    return JobCreated(
        job_id=job.id,
        status=job.status,
        total=job.total_count,
        accepted=len(valid),
        invalid=len(rejected),
        invalid_recipients=[InvalidRecipient(index=r.index, errors=r.errors) for r in rejected],
        status_url=str(request.url_for("get_job", job_id=job.id)),
        certificates_url=str(request.url_for("list_certificates", job_id=job.id)),
    )


@router.get("/jobs/{job_id}", response_model=JobStatusOut, summary="Job status and progress")
def get_job(job_id: str, request: Request, db: Session = Depends(get_db)) -> JobStatusOut:
    job = _get_job(db, job_id)
    counts = status_counts(db, job.id)

    failures = db.scalars(
        select(Certificate)
        .where(
            Certificate.job_id == job.id,
            Certificate.status.in_([CertificateStatus.FAILED, CertificateStatus.INVALID]),
        )
        .order_by(Certificate.row_index)
        .limit(MAX_FAILURES_IN_STATUS + 1)
    ).all()

    return JobStatusOut(
        job_id=job.id,
        status=job.status,
        certificate=CertificateInfo(
            title=job.title,
            course_name=job.course_name,
            issuer_name=job.issuer_name,
            issue_date=job.issue_date,
        ),
        progress=_progress(counts),
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        failures=[
            FailureItem(
                certificate_id=c.id,
                index=c.row_index,
                recipient_name=c.recipient_name,
                status=c.status,
                error=c.error,
            )
            for c in failures[:MAX_FAILURES_IN_STATUS]
        ],
        failures_truncated=len(failures) > MAX_FAILURES_IN_STATUS,
        certificates_url=str(request.url_for("list_certificates", job_id=job.id)),
        download_url=(
            str(request.url_for("download_job", job_id=job.id))
            if counts[CertificateStatus.GENERATED]
            else None
        ),
    )


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobStatusOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry certificates whose generation failed",
)
def retry_job(job_id: str, request: Request, db: Session = Depends(get_db)) -> JobStatusOut:
    job = _get_job(db, job_id)
    if not job.status.is_finished:
        raise HTTPException(status.HTTP_409_CONFLICT, "Job is still in progress")
    if reset_failed_certificates(db, job) == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "Job has no failed certificates to retry")
    runner.submit(job.id)
    db.expire_all()
    return get_job(job_id, request, db)


@router.get(
    "/jobs/{job_id}/certificates",
    response_model=CertificatePage,
    summary="List certificates of a job (paginated, filterable by status)",
)
def list_certificates(
    job_id: str,
    request: Request,
    status_filter: CertificateStatus | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> CertificatePage:
    _get_job(db, job_id)
    conditions = [Certificate.job_id == job_id]
    if status_filter is not None:
        conditions.append(Certificate.status == status_filter)

    total = db.scalar(select(func.count()).select_from(Certificate).where(*conditions))
    certs = db.scalars(
        select(Certificate)
        .where(*conditions)
        .order_by(Certificate.row_index)
        .limit(limit)
        .offset(offset)
    ).all()
    return CertificatePage(
        items=[_certificate_out(request, c) for c in certs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/jobs/{job_id}/download",
    response_class=FileResponse,
    summary="Download all generated certificates of a finished job as a ZIP",
)
def download_job(job_id: str, db: Session = Depends(get_db)) -> FileResponse:
    job = _get_job(db, job_id)
    if not job.status.is_finished:
        raise HTTPException(status.HTTP_409_CONFLICT, "Job is still in progress")

    certs = db.scalars(
        select(Certificate)
        .where(Certificate.job_id == job.id, Certificate.status == CertificateStatus.GENERATED)
        .order_by(Certificate.row_index)
    ).all()
    if not certs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job has no generated certificates")

    # Build the archive on disk (not in memory) - large jobs can be hundreds of MB.
    fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(tmp_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for cert in certs:
            archive.write(
                cert.file_path, arcname=f"{cert.row_index + 1:05d}-{_slug(cert.recipient_name)}.pdf"
            )

    return FileResponse(
        tmp_name,
        media_type="application/zip",
        filename=f"certificates-{job.id}.zip",
        background=BackgroundTask(os.remove, tmp_name),
    )


# ---------------------------------------------------------------------- certificates


@router.get(
    "/certificates/{certificate_id}",
    response_model=CertificateOut,
    summary="Certificate details",
)
def get_certificate(
    certificate_id: str, request: Request, db: Session = Depends(get_db)
) -> CertificateOut:
    cert = db.get(Certificate, certificate_id)
    if cert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Certificate {certificate_id} not found")
    return _certificate_out(request, cert)


@router.get(
    "/certificates/{certificate_id}/download",
    response_class=FileResponse,
    summary="Download a generated certificate PDF",
)
def download_certificate(certificate_id: str, db: Session = Depends(get_db)) -> FileResponse:
    cert = db.get(Certificate, certificate_id)
    if cert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Certificate {certificate_id} not found")
    if cert.status != CertificateStatus.GENERATED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Certificate is not available (status: {cert.status.value})",
        )
    path = Path(cert.file_path)
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "Certificate file is missing from storage")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"certificate-{_slug(cert.recipient_name)}-{cert.id[:8]}.pdf",
    )
