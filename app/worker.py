"""Background processing of generation jobs.

Design: the API only *records* a job (job row + one certificate row per
recipient) and hands the job id to a worker. All state lives in the database,
so the worker needs nothing but the job id. That makes it straightforward to
replace the in-process thread pool with Celery/RQ/etc. later: ``process_job``
becomes the task body unchanged.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import generator
from app.config import settings
from app.database import SessionLocal
from app.models import Certificate, CertificateStatus, Job, JobStatus, utcnow

logger = logging.getLogger(__name__)

# Pending certificates are loaded in chunks so a 10k-recipient job never holds
# every row in memory at once.
BATCH_SIZE = 100

# Set on application shutdown. Workers stop between certificates and leave the
# job in "processing"; it is picked up again by ``resume_unfinished_jobs``.
_stop_event = threading.Event()


def certificate_path(job_id: str, certificate_id: str) -> Path:
    return settings.storage_dir / job_id / f"{certificate_id}.pdf"


def status_counts(db: Session, job_id: str) -> dict[CertificateStatus, int]:
    """Number of certificates per status, computed from the rows themselves.

    Counting (instead of keeping counters on the job row) means progress can
    never drift from reality, and there are no counter races between threads.
    """
    rows = db.execute(
        select(Certificate.status, func.count())
        .where(Certificate.job_id == job_id)
        .group_by(Certificate.status)
    ).all()
    counts = {status: 0 for status in CertificateStatus}
    for status, count in rows:
        counts[CertificateStatus(status)] = count
    return counts


def final_status(counts: dict[CertificateStatus, int]) -> JobStatus:
    generated = counts[CertificateStatus.GENERATED]
    unsuccessful = counts[CertificateStatus.FAILED] + counts[CertificateStatus.INVALID]
    if generated == 0:
        return JobStatus.FAILED
    if unsuccessful:
        return JobStatus.COMPLETED_WITH_ERRORS
    return JobStatus.COMPLETED


def finalize_job(db: Session, job: Job) -> None:
    job.status = final_status(status_counts(db, job.id))
    job.completed_at = utcnow()
    db.commit()


def _generate_one(db: Session, job: Job, cert: Certificate) -> None:
    """Generate a single certificate, isolating any failure to this certificate."""
    cert.attempts += 1
    try:
        path = certificate_path(job.id, cert.id)
        generator.render_certificate(
            generator.CertificateData(
                certificate_id=cert.id,
                recipient_name=cert.recipient_name or "",
                title=job.title,
                course_name=job.course_name,
                issuer_name=job.issuer_name,
                issue_date=job.issue_date,
            ),
            path,
        )
    except Exception as exc:  # noqa: BLE001 - any rendering error fails only this certificate
        logger.exception("Certificate %s (job %s) failed", cert.id, job.id)
        cert.status = CertificateStatus.FAILED
        cert.error = f"{type(exc).__name__}: {exc}"
    else:
        cert.status = CertificateStatus.GENERATED
        cert.error = None
        cert.file_path = str(path)
        cert.generated_at = utcnow()
    # Commit per certificate so progress is visible to pollers immediately and
    # finished work survives a crash part-way through the job.
    db.commit()


def process_job(job_id: str) -> None:
    """Generate every pending certificate of a job, then set its final status."""
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            logger.warning("Job %s disappeared before processing", job_id)
            return
        if job.status.is_finished:
            return

        job.status = JobStatus.PROCESSING
        job.started_at = job.started_at or utcnow()
        job.error = None
        db.commit()

        try:
            while True:
                batch = db.scalars(
                    select(Certificate)
                    .where(
                        Certificate.job_id == job_id,
                        Certificate.status == CertificateStatus.PENDING,
                    )
                    .order_by(Certificate.row_index)
                    .limit(BATCH_SIZE)
                ).all()
                if not batch:
                    break
                for cert in batch:
                    if _stop_event.is_set():
                        logger.info("Shutdown requested; job %s will resume on restart", job_id)
                        return
                    _generate_one(db, job, cert)
            finalize_job(db, job)
        except Exception as exc:  # noqa: BLE001 - infrastructure failure (DB, disk...)
            logger.exception("Job %s crashed", job_id)
            db.rollback()
            job = db.get(Job, job_id)
            job.status = JobStatus.FAILED
            job.error = f"Job processing aborted: {type(exc).__name__}: {exc}"
            job.completed_at = utcnow()
            db.commit()


class JobRunner:
    """Dispatches jobs to a bounded thread pool (or runs them inline in sync mode)."""

    def __init__(self) -> None:
        self._executor: ThreadPoolExecutor | None = None

    def _get_executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=settings.worker_threads, thread_name_prefix="cert-worker"
            )
        return self._executor

    def start(self) -> None:
        _stop_event.clear()

    def submit(self, job_id: str) -> None:
        if settings.sync_processing:
            process_job(job_id)
        else:
            self._get_executor().submit(process_job, job_id)

    def shutdown(self, wait: bool = True) -> None:
        _stop_event.set()
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
            self._executor = None


runner = JobRunner()


def resume_unfinished_jobs() -> list[str]:
    """Re-queue jobs that were pending/processing when the process last stopped.

    Generated certificates are already committed, so a resumed job only
    processes the certificates that are still ``pending``.
    """
    with SessionLocal() as db:
        job_ids = db.scalars(
            select(Job.id).where(Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]))
        ).all()
    for job_id in job_ids:
        logger.info("Resuming unfinished job %s", job_id)
        runner.submit(job_id)
    return list(job_ids)


def reset_failed_certificates(db: Session, job: Job) -> int:
    """Mark failed (not invalid) certificates as pending again so they are retried."""
    result = db.execute(
        update(Certificate)
        .where(Certificate.job_id == job.id, Certificate.status == CertificateStatus.FAILED)
        .values(status=CertificateStatus.PENDING, error=None)
    )
    if result.rowcount:
        job.status = JobStatus.PENDING
        job.completed_at = None
    db.commit()
    return result.rowcount
