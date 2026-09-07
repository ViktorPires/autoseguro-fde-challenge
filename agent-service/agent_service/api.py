import asyncio
import sqlite3
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings
from .events import Events
from .language import create_language_adapter
from .quote_client import QuoteClient
from .repository import Conflict, Repository
from .schemas import ChatRequest, ChatResponse
from .workflow import Workflow


def create_app(
    settings: Settings | None = None,
    *,
    language=None,
    quote_transport=None,
    provider_transport=None,
):
    @asynccontextmanager
    async def lifespan(app):
        cfg = settings or Settings()
        app.state.settings = cfg
        event = Events(cfg)
        repo = Repository(cfg.sqlite_path, cfg.hmac_secret.get_secret_value())
        app.state.repo, app.state.event, app.state.ready = repo, event, False
        try:
            repo.initialize()
            recovered = repo.recover()
            repo.writable()
            app.state.ready = True
            event("startup", outcome="recovered" if recovered else "ready")
        except (sqlite3.Error, OSError, RuntimeError):
            event("storage_unavailable", error_code="storage_unavailable")
        async with (
            httpx.AsyncClient(
                base_url=str(cfg.quote_url),
                transport=quote_transport or httpx.AsyncHTTPTransport(retries=0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                follow_redirects=False,
                trust_env=False,
            ) as upstream,
            httpx.AsyncClient(
                transport=provider_transport or httpx.AsyncHTTPTransport(retries=0),
                follow_redirects=False,
                trust_env=False,
            ) as provider,
        ):
            adapter = language or create_language_adapter(provider, cfg, event)
            app.state.workflow = Workflow(
                repo, QuoteClient(upstream, cfg, event), adapter, cfg, event
            )
            yield
        app.state.ready = False

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        try:
            cid = UUID(request.headers.get("X-Correlation-ID", ""))
        except ValueError:
            cid = uuid4()
        request.state.correlation_id = str(cid)
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = str(cid)
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        app.state.event(
            "invalid_request",
            correlation_id=request.state.correlation_id,
            error_code="invalid_request",
        )
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.get("/health")
    async def health():
        cfg = app.state.settings
        return {"status": "ok", "version": cfg.app_version, "git_sha": cfg.git_sha}

    @app.get("/ready")
    async def ready():
        healthy = app.state.ready
        try:
            if healthy:
                app.state.repo.writable()
        except (sqlite3.Error, OSError):
            healthy = False
        return JSONResponse(
            {"status": "ready" if healthy else "unavailable"},
            status_code=200 if healthy else 503,
        )

    @app.post("/api/v1/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request):
        cid, mid, correlation_id = (
            str(body.conversation_id),
            str(body.message_id),
            request.state.correlation_id,
        )
        repo, event, cfg = app.state.repo, app.state.event, app.state.settings
        if not app.state.ready:
            return JSONResponse({"error": "storage_unavailable"}, status_code=503)
        claimed = False
        try:
            saved = repo.claim(body, correlation_id)
            if saved is not None:
                event(
                    "message_replay",
                    conversation_id=cid,
                    message_id=mid,
                    correlation_id=correlation_id,
                    original_correlation_id=saved["correlation_id"],
                )
                return JSONResponse(saved)
            claimed = True
            event(
                "message_claimed",
                conversation_id=cid,
                message_id=mid,
                correlation_id=correlation_id,
            )
            async with asyncio.timeout(cfg.turn_deadline - cfg.finalization_reserve):
                return await app.state.workflow.run(body, correlation_id)
        except Conflict as exc:
            event(
                "message_conflict",
                correlation_id=correlation_id,
                conversation_id=cid,
                message_id=mid,
                error_code=exc.code,
            )
            return JSONResponse(
                {
                    "error": exc.code,
                    "retry": "Retry the same IDs and content later."
                    if exc.code != "idempotency_conflict"
                    else "Use a new message ID for changed content.",
                },
                status_code=409,
            )
        except (TimeoutError, asyncio.CancelledError):
            try:
                saved = repo.interrupt(cid, mid)
                event(
                    "processing_interrupted",
                    correlation_id=correlation_id,
                    conversation_id=cid,
                    message_id=mid,
                    handoff_id=saved["handoff"]["id"],
                )
                return saved
            except (sqlite3.Error, OSError):
                app.state.ready = False
                return JSONResponse({"error": "storage_unavailable"}, status_code=503)
        except (sqlite3.Error, OSError):
            app.state.ready = False
            event(
                "storage_unavailable",
                correlation_id=correlation_id,
                error_code="storage_unavailable",
            )
            return JSONResponse({"error": "storage_unavailable"}, status_code=503)
        except Exception:
            # Never allow arbitrary provider/validation exception text into logs.
            event(
                "internal_error",
                correlation_id=correlation_id,
                error_code="internal_error",
            )
            try:
                if claimed:
                    return repo.interrupt(cid, mid)
            except (sqlite3.Error, OSError):
                app.state.ready = False
            return JSONResponse({"error": "processing_unavailable"}, status_code=503)

    return app
