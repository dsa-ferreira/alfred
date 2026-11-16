"""A background model request that gives way to the queue.

Used by summaries, fact extraction and the duplicate check. The request is
streamed, because the model server only stops generating for a dropped client
on a streamed request; when ``preempt`` is set first, it is cancelled and
nothing comes back.
"""

from __future__ import annotations

import asyncio
from typing import Any

from alfred.infra.llm.chat import ModelReply
from alfred.infra.llm.protocols import ChatModel


async def complete_unless_preempted(
    model: ChatModel, messages: list[dict[str, Any]], preempt: asyncio.Event | None
) -> ModelReply | None:
    """The reply, or ``None`` when ``preempt`` was set before it came.

    ChatModel errors propagate. However this ends, a request still running is
    cancelled and awaited.
    """
    request = asyncio.ensure_future(model.complete_streamed(messages))
    try:
        if preempt is not None:
            waiter = asyncio.ensure_future(preempt.wait())
            try:
                await asyncio.wait({request, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
            if not request.done():
                return None
        return await request
    finally:
        if not request.done():
            # Preempted, or this task was cancelled: closing the stream
            # makes the model server stop generating.
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
