"""The attachments tables.

- ``attachments``: users' photos, image files and PDFs. The bytes are in
  the object store under ``object_key``; the row is written with its queue
  item and linked to the user entry (``entry_id``) when the turn records
  it.
- ``attachment_pages`` holds each PDF page's text.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
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
    __table_args__ = (
        CheckConstraint(in_values("kind", _ATTACHMENT_KINDS), name="kind"),
        Index("ix_attachments_queue_item_id", "queue_item_id", "position"),
        Index("ix_attachments_entry_id", "entry_id"),
    )

    # The number the model sees in a handle ("#7").
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("queue_items.id"), nullable=False
    )
    # Who sent it.
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    # The user entry of the message, once its turn has recorded it.
    entry_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("entries.id"))
    # Order within the message (an album).
    position: Mapped[int] = mapped_column(Integer, nullable=False)
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
        BigInteger, ForeignKey("attachments.id"), primary_key=True
    )
    # 1-based, as the model and users count them.
    page: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
