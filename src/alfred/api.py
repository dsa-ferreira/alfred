"""The HTTP API for homelab services: ``/notify``, ``/ask`` and ``/healthz``.

Services authenticate with a bearer token from ``[[services]]``; the matching
service's name identifies the sender.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from alfred.config import Service
from alfred.history import History
from alfred.queue import Outbox, Queue

log = logging.getLogger(__name__)

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class NotifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: NonEmpty


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt: NonEmpty


class AskAccepted(BaseModel):
    id: int


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def create_app(
    services: tuple[Service, ...], queue: Queue, history: History, outbox: Outbox
) -> FastAPI:
    app = FastAPI(title="alfred", docs_url=None, redoc_url=None, openapi_url=None)
    known = [(s.name, s.token.get_secret_value().encode()) for s in services]

    def service_name(request: Request) -> str:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise _unauthorized()
        presented = token.encode()
        match: str | None = None
        for name, expected in known:
            # Compare against every token, in constant time each.
            if hmac.compare_digest(presented, expected):
                match = name
        if match is None:
            log.warning("rejected a request with an unknown service token")
            raise _unauthorized()
        return match

    # A plain default, not an Annotated alias: annotations are strings here and
    # FastAPI can't resolve a name local to this function.
    caller = Depends(service_name)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/notify")
    async def notify(body: NotifyRequest, service: str = caller) -> JSONResponse:
        # Sent directly, never through the AI queue.
        try:
            await outbox.send(f"[{service}] {body.text}")
        except Exception as exc:
            log.warning("notify from %s not delivered: %s", service, type(exc).__name__)
            return JSONResponse(
                {"detail": "Telegram did not accept the message; retry later"},
                status_code=status.HTTP_502_BAD_GATEWAY,
            )
        await history.append("notice", body.text, source=service)
        log.info("notify from %s delivered", service)
        return JSONResponse({"status": "sent"})

    @app.post("/ask", status_code=status.HTTP_202_ACCEPTED)
    async def ask(body: AskRequest, service: str = caller) -> AskAccepted:
        item_id = await queue.enqueue_ask(service, body.prompt)
        log.info("ask from %s queued as item %s", service, item_id)
        return AskAccepted(id=item_id)

    return app
