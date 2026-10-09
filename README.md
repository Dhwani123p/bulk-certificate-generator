# Bulk Certificate Generator

A FastAPI service that accepts a list of recipients in **one request**, validates
each recipient, generates a PDF certificate for every valid one in the
background, and lets the client track progress and download the results
(one at a time or as a ZIP).

**Stack:** Python 3.11+ (tested on 3.14) · FastAPI · SQLAlchemy 2 · SQLite (default; any
SQLAlchemy-supported relational DB such as PostgreSQL works via `DATABASE_URL`) ·
ReportLab (PDF) · pytest

---

## Setup

```bash
cd bulk-certificate-generator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt     # runtime + test dependencies
```

## Running the application

```bash
uvicorn app.main:app --reload
```

- API: http://localhost:8000/api/v1
- Interactive docs (Swagger UI): http://localhost:8000/docs
- Tables and the storage folder are created automatically on startup.

### Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/certificates.db` | Any SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@host/db` (install the driver) |
| `STORAGE_DIR` | `./data/certificates` | Where generated PDFs are written |
| `MAX_RECIPIENTS_PER_JOB` | `10000` | Upper bound on recipients per request |
| `WORKER_THREADS` | `2` | How many jobs are processed concurrently |
| `SYNC_PROCESSING` | `false` | Process jobs inline in the request (used by tests / debugging) |

## Running the tests

```bash
pytest -q
```

Tests use a throwaway SQLite DB and temp storage dir, so they never touch `./data`.

| File | Covers |
|---|---|
| `tests/test_jobs.py` | job creation, status/progress (incl. mid-job), individual failure isolation, all-failed jobs, retry, real background mode, resume after crash |
| `tests/test_validation.py` | request-level 422s, recipient limit, per-recipient validation (blank/missing name, bad email, duplicates, non-objects, length, unsupported characters) |
| `tests/test_certificates.py` | PDF rendering content, long names, listing/filtering/pagination, single PDF download, ZIP download, error cases (404/409/410) |

---

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/jobs` | Submit a generation job → `202 Accepted` |
| `GET` | `/api/v1/jobs/{job_id}` | Status, progress counts, list of failures |
| `GET` | `/api/v1/jobs/{job_id}/certificates?status=&limit=&offset=` | Paginated certificates (filter by `pending`/`generated`/`failed`/`invalid`) |
| `GET` | `/api/v1/jobs/{job_id}/download` | ZIP of all generated PDFs (once the job finished) |
| `POST` | `/api/v1/jobs/{job_id}/retry` | Re-run certificates whose *generation* failed |
| `GET` | `/api/v1/certificates/{certificate_id}` | Certificate details |
| `GET` | `/api/v1/certificates/{certificate_id}/download` | The PDF |

### 1. Submit a certificate generation request

```bash
curl -s -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d @examples/request.json
```

Request body:

```json
{
  "certificate": {
    "title": "Certificate of Completion",      // optional, this is the default
    "course_name": "Introduction to Python",   // required
    "issuer_name": "Acme Academy",             // required
    "issue_date": "2026-10-01"                 // optional, defaults to today
  },
  "recipients": [
    { "name": "Alice Smith", "email": "alice@example.com" },
    { "name": "Bob Jones" }
  ]
}
```

Response (`202 Accepted`) – invalid recipients are reported immediately, by index:

```json
{
  "job_id": "8c0e…",
  "status": "processing",
  "total": 5,
  "accepted": 3,
  "invalid": 2,
  "invalid_recipients": [
    { "index": 3, "errors": ["name: String should have at least 1 character"] },
    { "index": 4, "errors": ["email: value is not a valid email address: …"] }
  ],
  "status_url": "http://localhost:8000/api/v1/jobs/8c0e…",
  "certificates_url": "http://localhost:8000/api/v1/jobs/8c0e…/certificates"
}
```

### 2. Track progress

```bash
curl -s http://localhost:8000/api/v1/jobs/<job_id>
```

```json
{
  "job_id": "8c0e…",
  "status": "completed_with_errors",
  "progress": { "total": 5, "pending": 0, "generated": 3, "failed": 0,
                "invalid": 2, "processed": 5, "percent": 100.0 },
  "failures": [
    { "certificate_id": "…", "index": 3, "recipient_name": null, "status": "invalid", "error": "name: …" },
    { "certificate_id": "…", "index": 4, "recipient_name": "Bad Email", "status": "invalid", "error": "email: …" }
  ],
  "failures_truncated": false,
  "download_url": "http://localhost:8000/api/v1/jobs/8c0e…/download",
  ...
}
```

Job statuses: `pending` → `processing` → one of `completed` (all generated),
`completed_with_errors` (some generated, some failed/invalid), `failed` (nothing generated).

Certificate statuses: `pending`, `generated`, `failed` (generation error – retryable),
`invalid` (bad input – never generated).

### 3. Retrieve generated certificates

```bash
# list (with download links)
curl -s "http://localhost:8000/api/v1/jobs/<job_id>/certificates?status=generated&limit=100"

# a single PDF
curl -OJ http://localhost:8000/api/v1/certificates/<certificate_id>/download

# everything as a ZIP (files named 00001-alice-smith.pdf, ... by submission order)
curl -OJ http://localhost:8000/api/v1/jobs/<job_id>/download
```

---

## Design decisions

### Background processing (not synchronous)

`POST /jobs` validates, stores one row per recipient, **commits**, hands the job id
to a worker and returns `202` right away. The client polls the status endpoint.

- *Why not synchronous:* a request with thousands of recipients would hold an
  HTTP connection open for minutes and hit client/proxy timeouts; one slow job
  would also tie up a server worker.
- *Why an in-process `ThreadPoolExecutor` rather than Celery/RQ:* it needs no extra
  infrastructure (no Redis/RabbitMQ) for this assignment, and the pool is bounded
  (`WORKER_THREADS`) so many simultaneous jobs queue up instead of overloading the
  machine. PDF rendering is fast: 500 certificates took about 2 s locally.
- *Easy to swap:* all state lives in the DB and the worker needs only a job id,
  so `worker.process_job(job_id)` can become a Celery task body unchanged. That is
  the next step for multi-process / multi-machine deployments.
- *Trade-off:* jobs live in the API process. To mitigate this:
  - each certificate is **committed individually**, so finished work is never lost and
    progress is live;
  - on startup, `resume_unfinished_jobs()` re-queues any `pending`/`processing` jobs,
    and only their still-`pending` certificates are processed;
  - on shutdown, workers stop between certificates and the job is resumed on the
    next start.
  - With several uvicorn workers/processes, each process would run the resume
    step – a real queue (or a DB row lock / "claimed_by" column) would be needed there.

### Two levels of validation

1. **Request level → `422`:** missing/invalid certificate info, `recipients` not a
   non-empty list, more than `MAX_RECIPIENTS_PER_JOB` entries. Nothing sensible can be
   generated, so the whole request is rejected.
2. **Recipient level → recorded as `invalid`:** each entry is validated on its own
   (`app/validation.py`): object with a non-blank `name` ≤ 100 chars, optional valid
   `email`, no duplicate emails within a job, no control characters, and only characters
   the template font can render. Invalid entries are reported in the create response and
   stored as certificates with status `invalid` and an error message, so one bad row in
   a CSV export doesn't block hundreds of good ones. `recipients` is typed `list[Any]`
   in the request model precisely so this per-entry handling is possible.

### Failure isolation

`_generate_one` wraps rendering in its own `try/except`; an exception marks only that
certificate `failed` with the error message and processing continues. A separate
outer handler marks the whole job `failed` (with `job.error`) only for infrastructure
errors such as the DB becoming unavailable. Failed certificates can be retried with
`POST /jobs/{id}/retry`; invalid ones cannot (the input itself must be fixed and
resubmitted).

### Progress is computed, not stored

Counts come from `GROUP BY status` over the certificate rows (indexed on
`(job_id, status)`), rather than counters on the job row. They can never drift
from reality and there are no lost-update races between threads.

### Other details

- **Template:** a single landscape A4 design drawn with ReportLab (`app/generator.py`),
  containing title, recipient name, course, date, issuer and a unique certificate ID.
  Long names/courses are shrunk to fit. `render_certificate` is a pure function, so it is
  easy to test or replace (e.g. HTML + WeasyPrint).
- **Atomic writes:** PDFs are written to a temp file then `os.replace`d, so a partially
  written file is never served.
- **Storage:** files on local disk under `STORAGE_DIR/<job_id>/<certificate_id>.pdf`;
  only the path is stored in the DB. Swapping in S3 would mean changing `render_certificate`'s
  destination and returning pre-signed URLs from the download endpoints.
- **Large jobs:** pending rows are fetched in batches of 100; the ZIP is built in a temp
  file on disk (not memory); the certificate listing is paginated; the status response
  lists at most 100 failures (`failures_truncated` tells you to page through
  `/certificates?status=failed`).
- **IDs** are UUIDs so they are not guessable/enumerable.

### Known limitations / possible improvements

- No authentication/authorization – any client with an ID can read a job.
- The built-in PDF fonts support Latin characters only (cp1252); other scripts are
  rejected at validation time. Embedding a Unicode TTF font would remove this.
- No idempotency key – resubmitting the same request creates a second job.
- No retention policy for generated files.
- SQLite is fine for a single process; use PostgreSQL for concurrent writers.

## Project layout

```
app/
  main.py        FastAPI app, startup (create tables, resume jobs) / shutdown
  api.py         HTTP endpoints
  schemas.py     Pydantic request/response models + field validation
  validation.py  Per-recipient validation (valid vs rejected)
  models.py      SQLAlchemy models: Job, Certificate, status enums
  worker.py      Job processing, thread pool, resume, retry
  generator.py   The certificate template (ReportLab PDF)
  database.py    Engine/session setup
  config.py      Settings from environment variables
tests/           pytest suite
examples/        Sample request body
```
