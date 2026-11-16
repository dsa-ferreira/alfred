"""The HTTP API for homelab services: ``/notify``, ``/ask`` and ``/healthz``.

Services authenticate with a bearer token from ``[[services]]``; the
matching service's name identifies the sender. ``/notify`` and ``/ask`` take
an optional ``user``, the configured user they are for; without it they are
for the admin, and an unknown name is refused with 422.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from alfred.config import Service
from alfred.conversations.store import History
from alfred.kernel.outbox import Outbox
from alfred.kernel.users import User, Users
from alfred.turns.queue import Queue

log = logging.getLogger(__name__)

_NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def create_app(
    services: tuple[Service, ...],
    users: Users,
    queue: Queue,
    history: History,
    outbox: Outbox,
) -> FastAPI:
    app = FastAPI(title="alfred", docs_url=None, redoc_url=None, openapi_url=None)
    known = [(s.name, s.token.get_secret_value().encode()) for s in services]

    def target(name: str | None) -> User:
        """The user a call is for: the one named, or the admin."""
        if name is None:
            return users.admin
        user = users.get(name)
        if user is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown user {name!r}")
        return user

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
    async def notify(body: _NotifyRequest, service: str = caller) -> JSONResponse:
        user = target(body.user)
        # Sent directly, never through the AI queue.
        try:
            await outbox.send(user, f"[{service}] {body.text}")
        except Exception as exc:
            log.warning(
                "notify from %s to %s not delivered: %s", service, user.name, type(exc).__name__
            )
            return JSONResponse(
                {"detail": "Telegram did not accept the message; retry later"},
                status_code=status.HTTP_502_BAD_GATEWAY,
            )
        await history.append(user.name, "notice", body.text, source=service)
        log.info("notify from %s to %s delivered", service, user.name)
        return JSONResponse({"status": "sent"})

    @app.post("/ask", status_code=status.HTTP_202_ACCEPTED)
    async def ask(body: _AskRequest, service: str = caller) -> _AskAccepted:
        user = target(body.user)
        item_id = await queue.enqueue_ask(service, body.prompt, user.name)
        log.info("ask from %s for %s queued as item %s", service, user.name, item_id)
        return _AskAccepted(id=item_id)

    return app


class _NotifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: _NonEmpty
    user: str | None = None


class _AskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt: _NonEmpty
    user: str | None = None


class _AskAccepted(BaseModel):
    id: int


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )
