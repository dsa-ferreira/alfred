"""The model servers' settings: the chat model's part of ``[ai]`` and the
embedding server's part of ``[memory]``."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from alfred.settings import HttpUrlStr, PositiveFloat, Section


class ChatConfig(Section):
    base_url: HttpUrlStr = "http://ai.lan"
    request_timeout_s: PositiveFloat = 300


class EmbeddingConfig(Section):
    embedding_base_url: HttpUrlStr = "http://ragman.lan"
    embedding_model: Annotated[str, Field(min_length=1)] = "nomic-embed-text:v1.5"
    embedding_timeout_s: PositiveFloat = 30
    document_prefix: str = "search_document: "
    query_prefix: str = "search_query: "
