"""The embedding server's part of ``[memory]``."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from alfred.settings import HttpUrlStr, PositiveFloat, Section


class EmbeddingConfig(Section):
    embedding_base_url: HttpUrlStr = "http://ragman.lan"
    embedding_model: Annotated[str, Field(min_length=1)] = "nomic-embed-text:v1.5"
    embedding_timeout_s: PositiveFloat = 30
    document_prefix: str = "search_document: "
    query_prefix: str = "search_query: "
