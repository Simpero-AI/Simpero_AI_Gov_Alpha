from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import (
    admin,
    auth,
    deals,
    health,
    history,
    inspector,
    intake_questions,
    investment_profile,
    logs,
    mandates,
    public_intake,
    public_uploads,
    uploads,
)
from app.core.config import get_settings
from app.core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    TenantContextError,
)
from app.core.rate_limit_middleware import RateLimitMiddleware

settings = get_settings()

app = FastAPI(
    title="Simpero API",
    version="0.1.0",
    description="AI-powered due diligence platform — backend API",
)

# Starlette's add_middleware inserts each new middleware at position 0 of its
# internal list, and the stack is built by wrapping in reversed() order -- so
# the LAST-registered middleware ends up OUTERMOST (sees the request first,
# the response last). RateLimitMiddleware must be registered BEFORE
# CORSMiddleware so CORS stays outermost: a 429 short-circuit from the
# limiter still passes through CORSMiddleware on the way out (so the browser
# sees a readable 429, not an opaque CORS failure), and CORS preflight
# OPTIONS requests are fully handled by CORSMiddleware before ever reaching
# the limiter (so they're never wastefully counted against the rate limit).
# Do not reorder these two calls.
app.add_middleware(RateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Origins are loaded from CORS_ALLOWED_ORIGINS in .env — never hardcode them here.
)


@app.exception_handler(AuthenticationError)
async def authentication_error_handler(request: Request, exc: AuthenticationError) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": str(exc)})


@app.exception_handler(AuthorizationError)
async def authorization_error_handler(request: Request, exc: AuthorizationError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(TenantContextError)
async def tenant_context_error_handler(request: Request, exc: TenantContextError) -> JSONResponse:
    # 401 not 400: missing tenant context is an auth failure, not a malformed request.
    return JSONResponse(status_code=401, content={"detail": str(exc)})


# Single /api mount point, kept here rather than per-router: the frontend's
# dev proxy (vite.config.ts) and documented prod ingress both forward only
# /api/* to this service — every route must live under it.
API_PREFIX = "/api"
app.include_router(health.router, prefix=API_PREFIX)
app.include_router(deals.router, prefix=API_PREFIX)
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(history.router, prefix=API_PREFIX)
app.include_router(investment_profile.router, prefix=API_PREFIX)
app.include_router(logs.router, prefix=API_PREFIX)
app.include_router(mandates.router, prefix=API_PREFIX)
app.include_router(intake_questions.router, prefix=API_PREFIX)
app.include_router(public_intake.router, prefix=API_PREFIX)
app.include_router(public_uploads.router, prefix=API_PREFIX)
app.include_router(admin.router, prefix=API_PREFIX)
app.include_router(uploads.router, prefix=API_PREFIX)
app.include_router(inspector.router, prefix=API_PREFIX)


@app.exception_handler(404)
async def not_found_json_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Guarantee every 404 is JSON, never the ingress SPA's index.html — so an
    orphaned /api call (e.g. the retired /api/trpc/investmentProfile.upsert the
    mandate/firm-profile UI used to POST to, or any future dead route) fails as
    parseable JSON rather than an "Unexpected token '<'" HTML-parse error in the
    caller.

    A 404 exception handler, not a greedy /api/{path:path} route, precisely
    because it fires only AFTER routing on a genuine 404: real routes keep their
    405 (wrong method, with the Allow header) and their trailing-slash 307
    redirects, which a catch-all route would swallow into 404s. exc.detail is
    preserved, so the public-intake "Not found" contract and FastAPI's own
    default "Not Found" both pass through unchanged."""
    return JSONResponse(
        status_code=404,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


# Do not open DB connections at startup. PgBouncer transaction pooling requires sessions to be
# opened per-transaction, not per-application-lifecycle. A startup DB connection would hold a
# PgBouncer slot open indefinitely and bypass the pooling contract.
