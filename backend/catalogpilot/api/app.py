"""Application factory: middleware, error mapping, lifecycle.

Run locally:  uvicorn catalogpilot.api.app:app --reload   (from the backend directory)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__, db
from ..catalog.service import NotFound, VersionConflict
from ..config import get_settings
from ..datahub.service import UploadError
from ..engine.worker import WorkerThread
from ..identity.service import AuthError
from ..ids import uuid7
from ..security.authz import Forbidden
from ..security.passwords import WeakPassword
from . import auth_routes, routes
from .deps import Problem, problem_response

log = logging.getLogger("catalogpilot.api")
MAX_JSON_BODY = 1024 * 1024

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-site",
    # The API serves JSON and downloads only; nothing it returns should ever run as a page.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Cache-Control": "no-store",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.validate_for_production()
    db.create_all()
    worker = None
    if os.environ.get("CP_WORKER", "1") == "1":
        worker = WorkerThread()
        worker.start()
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()
            worker.join(timeout=30)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="CatalogPilot API", version=__version__, lifespan=lifespan,
        # Interactive docs are a development aid; production serves no undocumented surface (SEC-API-08).
        docs_url=None if settings.is_production else "/docs", redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.allowed_origins, allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "If-Match", "Upload-Offset",
                       "Upload-Checksum", "Authorization"],
        expose_headers=["ETag", "Upload-Offset", "Upload-Complete", "Upload-Length", "X-Request-Id", "Retry-After"],
    )

    @app.middleware("http")
    async def envelope(request: Request, call_next):
        request.state.request_id = f"req_{uuid7()}"
        length = request.headers.get("content-length")
        is_chunk = request.method == "PATCH" and "/files/" in request.url.path
        if length and length.isdigit() and int(length) > MAX_JSON_BODY and not is_chunk:
            response = problem_response(request, 413, "payload_too_large", "Request body is too large.")
        else:
            try:
                response = await call_next(request)
            except Exception:  # last resort: never leak internals (SEC-WEB-10)
                log.exception("unhandled error %s", request.state.request_id)
                response = problem_response(request, 500, "internal_error",
                                            "Something went wrong on our side. Quote the request id to support.")
        response.headers["X-Request-Id"] = request.state.request_id
        for name, value in SECURITY_HEADERS.items():
            if name == "Content-Security-Policy" and request.url.path in ("/docs", "/openapi.json"):
                continue
            response.headers.setdefault(name, value)
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")
        return response

    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem):
        return problem_response(request, exc.status, exc.code, exc.detail, exc.extra, exc.headers)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return problem_response(request, 403, exc.code, exc.detail)

    @app.exception_handler(AuthError)
    async def _auth(request: Request, exc: AuthError):
        return problem_response(request, exc.status, exc.code, exc.detail, exc.extra)

    @app.exception_handler(UploadError)
    async def _upload(request: Request, exc: UploadError):
        return problem_response(request, exc.status, exc.code, exc.detail, exc.extra)

    @app.exception_handler(WeakPassword)
    async def _weak(request: Request, exc: WeakPassword):
        return problem_response(request, 422, "weak_password", str(exc))

    @app.exception_handler(NotFound)
    async def _missing(request: Request, exc: NotFound):
        # "Not found" and "not yours" are indistinguishable on purpose (no existence leaks).
        return problem_response(request, 404, "not_found", "Not found.")

    @app.exception_handler(db.TenantIsolationError)
    async def _isolation(request: Request, exc: db.TenantIsolationError):
        log.error("tenant isolation guard triggered: %s (%s)", exc, request.state.request_id)
        return problem_response(request, 404, "not_found", "Not found.")

    @app.exception_handler(VersionConflict)
    async def _conflict(request: Request, exc: VersionConflict):
        return problem_response(request, 412, "precondition_failed", "This item changed. Reload and try again.")

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"] if p != "body"), "code": e["type"]}
                  for e in exc.errors()[:50]]  # field names and codes only: submitted values are never echoed
        return problem_response(request, 422, "validation_failed", f"{len(errors)} field(s) are invalid.",
                                {"errors": errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "bad_request")
        return problem_response(request, exc.status_code, code, "Not found." if exc.status_code == 404 else str(exc.detail))

    app.include_router(auth_routes.router, prefix="/v1")
    app.include_router(routes.router, prefix="/v1")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
