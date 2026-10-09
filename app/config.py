"""Application settings, read from environment variables.

Kept as a plain mutable object (rather than pydantic-settings) so tests can
override individual values at runtime without extra dependencies.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/certificates.db")
    )
    storage_dir: Path = field(
        default_factory=lambda: Path(os.getenv("STORAGE_DIR", "./data/certificates"))
    )
    # Upper bound on recipients in a single request; protects memory / DB.
    max_recipients_per_job: int = field(
        default_factory=lambda: int(os.getenv("MAX_RECIPIENTS_PER_JOB", "10000"))
    )
    # Number of jobs that can be processed concurrently by the worker pool.
    worker_threads: int = field(default_factory=lambda: int(os.getenv("WORKER_THREADS", "2")))
    # When true, jobs are processed inline inside the request (used by tests).
    sync_processing: bool = field(default_factory=lambda: _env_bool("SYNC_PROCESSING", False))


settings = Settings()
