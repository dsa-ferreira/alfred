"""A Bot whose API session is mocked: requests are recorded, answers scripted."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetFile, SendMessage, TelegramMethod
from aiogram.types import Chat, File, Message, Update

OWNER = 42
TOKEN = "123456:TEST-token"


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        # Called with each request; may raise to simulate Telegram errors.
        self.on_request: Callable[[TelegramMethod[Any]], None] | None = None
        # Files the owner sent, by file_id, for getFile and downloads.
        self.files: dict[str, bytes] = {}
        self.downloaded: list[str] = []  # the URLs streamed

    # Signature fixed by BaseSession, including its ``timeout`` parameter.
    async def make_request(  # type: ignore[no-untyped-def]
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109
    ):
        self.requests.append(method)
        if self.on_request is not None:
            self.on_request(method)
        if isinstance(method, GetFile):
            data = self.files.get(method.file_id)
            return File(
                file_id=method.file_id,
                file_unique_id=method.file_id,
                file_size=None if data is None else len(data),
                file_path=f"documents/{method.file_id}",
            )
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=0,
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        return True

    async def stream_content(  # type: ignore[override]
        self, url: str, *args: Any, **kwargs: Any
    ) -> AsyncGenerator[bytes]:
        self.downloaded.append(url)
        file_id = url.rsplit("/", 1)[-1]
        if file_id not in self.files:
            raise RuntimeError(f"Telegram refused {url}")  # quotes the URL, token and all
        data = self.files[file_id]
        for start in range(0, len(data), 1000):
            yield data[start : start + 1000]

    async def close(self) -> None:
        pass

    def sent(self) -> list[SendMessage]:
        return [r for r in self.requests if isinstance(r, SendMessage)]


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot(TOKEN, session=session), session


_update_id = 0


def message_update(*, user_id: int = OWNER, chat_type: str = "private", **content: Any) -> Update:
    global _update_id
    _update_id += 1
    chat_id = user_id if chat_type == "private" else -100123
    return Update.model_validate(
        {
            "update_id": _update_id,
            "message": {
                "message_id": _update_id,
                "date": 0,
                "chat": {"id": chat_id, "type": chat_type},
                "from": {"id": user_id, "is_bot": False, "first_name": "X"},
                **content,
            },
        }
    )
