"""The people alfred serves, as configured in ``[[users]]``."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from pydantic import SecretStr

from alfred.kernel.access import AllowList


@dataclass(frozen=True)
class User:
    """Someone alfred serves."""

    name: str
    telegram_user_id: int
    address: str = "sir"
    admin: bool = False
    tools: AllowList = AllowList()
    # MCP server name -> this user's token, where it isn't the server's own.
    mcp_tokens: Mapping[str, SecretStr] = field(default_factory=dict)
    # The tools whose calls wait for this user's confirmation.
    confirm: AllowList = AllowList()


@dataclass(frozen=True)
class Users:
    """The configured users, in file order. Exactly one is the admin."""

    all: tuple[User, ...]

    def __iter__(self) -> Iterator[User]:
        return iter(self.all)

    def __len__(self) -> int:
        return len(self.all)

    @property
    def names(self) -> list[str]:
        return [u.name for u in self.all]

    @property
    def admin(self) -> User:
        return next(u for u in self.all if u.admin)

    def get(self, name: str) -> User | None:
        return next((u for u in self.all if u.name == name), None)

    def by_telegram_id(self, telegram_user_id: int) -> User | None:
        return next((u for u in self.all if u.telegram_user_id == telegram_user_id), None)
