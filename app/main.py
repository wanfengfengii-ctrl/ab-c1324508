"""HTTP API for the radiotherapy dose-control platform."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Path, Request
from fastapi.responses import JSONResponse

from . import db, service
from .schemas import _ID_PATTERN, CreateCourseRequest, DeliveryRequest


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Radiotherapy Dose Control Platform",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(service.ApiError)
def _api_error(_: Request, exc: service.ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.get("/health")
def health() -> dict:
    """Liveness + readiness: proves the process can touch the database."""
    conn = db.connect()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return {"status": "ok"}


@app.put("/api/courses/{course_id}")
def create_course(
    course_id: str = Path(pattern=_ID_PATTERN),
    payload: CreateCourseRequest = ...,
) -> JSONResponse:
    body, created = service.create_course(course_id, payload)
    return JSONResponse(status_code=201 if created else 200, content=body)


@app.post("/api/courses/{course_id}/deliveries")
def submit_delivery(
    course_id: str = Path(pattern=_ID_PATTERN),
    payload: DeliveryRequest = ...,
) -> dict:
    return service.submit_delivery(course_id, payload)


@app.get("/api/courses/{course_id}")
def get_course(course_id: str = Path(pattern=_ID_PATTERN)) -> dict:
    return service.get_course(course_id)
