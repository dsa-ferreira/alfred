"""The attachments tables.

- ``attachments``: users' photos, image files and PDFs. The bytes are in
  the object store under ``object_key``. The row is written with the
  message's queue item, whose payload lists it; ``conversations`` links it
  to the user entry once the turn records it.
- ``attachment_pages`` holds each PDF page's text.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from alfred.db.base import Base, in_values

_ATTACHMENT_KINDS = ("image", "pdf")


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (CheckConstraint(in_values("kind", _ATTACHMENT_KINDS), name="kind"),)

    # The number the model sees in a handle ("#7").
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Who sent it.
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    file_name: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AttachmentPage(Base):
    __tablename__ = "attachment_pages"

    attachment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey(Attachment.id), primary_key=True
    )
    # 1-based, as the model and users count them.
    page: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
