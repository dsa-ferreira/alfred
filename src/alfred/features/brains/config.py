"""The brains' part of ``[ai]``: the startup model and the aliases the admin can switch to."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, ValidationInfo, field_validator

from alfred.infra.settings import Name, Section


class BrainEntry(Section):
    """An ``[ai.models]`` entry written as a table."""

    model: Annotated[str, Field(min_length=1)]
    vision: bool = False


def _brain_entries(value: Any) -> Any:
    """``alias = "tag"`` is short for ``alias = { model = "tag" }``."""
    if not isinstance(value, Mapping):
        return value
    return {k: {"model": v} if isinstance(v, str) else v for k, v in value.items()}


class BrainsConfig(Section):
    model: Annotated[str, Field(min_length=1)] = "gemma4:26b-a4b-it-qat"
    # Whether the [ai] model can see images, when no alias names it.
    vision: bool = False
    # The brains the admin can switch between: alias -> model tag, or
    # alias -> { model = tag, vision = true }. Both are read as a BrainEntry.
    models: Annotated[dict[Name, BrainEntry], BeforeValidator(_brain_entries)] = {}

    @field_validator("models")
    @classmethod
    def _check_models(
        cls, models: dict[str, BrainEntry], info: ValidationInfo
    ) -> dict[str, BrainEntry]:
        by_tag: dict[str, list[str]] = {}
        for alias, entry in models.items():
            by_tag.setdefault(entry.model, []).append(alias)
        problems = [
            "aliases " + ", ".join(repr(a) for a in aliases) + f" all name {tag!r}"
            for tag, aliases in by_tag.items()
            if len(aliases) > 1
        ]
        # The startup model is a brain under its own tag when no alias names it.
        model = info.data.get("model")
        if isinstance(model, str) and model not in by_tag and model in models:
            problems.append(f"alias {model!r} is also the name of the [ai] model's brain")
        if problems:
            raise ValueError("; ".join(problems))
        return models
