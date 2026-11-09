"""The brains (models) the owner can switch between, and the current one (design D1).

The brains are the ``[ai.models]`` aliases plus the ``[ai] model`` tag, which
is listed under its own tag when no alias names it. The startup brain is the
one whose tag is ``[ai] model``. The current brain lives only in memory: a
restart goes back to the startup brain.

The owner switches brains with ``alfred_set_model`` (``alfred.tools.brains``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Brain:
    name: str
    tag: str


class Brains:
    def __init__(self, default_tag: str, models: Mapping[str, str] | None = None) -> None:
        brains = [Brain(alias, tag) for alias, tag in (models or {}).items()]
        if not any(b.tag == default_tag for b in brains):
            brains.insert(0, Brain(default_tag, default_tag))
        self.brains: tuple[Brain, ...] = tuple(brains)
        self.default = next(b for b in brains if b.tag == default_tag)
        self.current = self.default

    @property
    def names(self) -> list[str]:
        return [b.name for b in self.brains]

    @property
    def others(self) -> list[str]:
        """The brains other than the current one, in configured order."""
        return [b.name for b in self.brains if b != self.current]

    @property
    def tag(self) -> str:
        return self.current.tag

    @property
    def is_default(self) -> bool:
        return self.current == self.default

    def set(self, name: str) -> Literal["switched", "already", "unknown"]:
        brain = next((b for b in self.brains if b.name == name), None)
        if brain is None:
            return "unknown"
        if brain == self.current:
            return "already"
        self.current = brain
        return "switched"

    def fall_back(self) -> bool:
        """Make the startup brain current; false when it already was."""
        if self.is_default:
            return False
        self.current = self.default
        return True
