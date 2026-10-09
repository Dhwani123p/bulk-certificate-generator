import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.config import settings
from app.database import init_db
from app.worker import resume_unfinished_jobs, runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    runner.start()
    resume_unfinished_jobs()
    yield
    runner.shutdown(wait=True)


app = FastAPI(
    title="Bulk Certificate Generator",
    version="1.0.0",
    description="Submit a list of recipients and generate a PDF certificate for each one.",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
