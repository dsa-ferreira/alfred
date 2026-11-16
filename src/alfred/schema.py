"""The whole schema: every slice's tables on one metadata, for Alembic and startup.

A slice defines its tables in its own ``tables.py``; importing it here
registers them on :data:`metadata`.
"""

from __future__ import annotations

from alfred.attachments.tables import Attachment
from alfred.conversations.tables import Conversation
from alfred.db.base import Base
from alfred.facts import tables as _facts  # noqa: F401  (registers its tables)
from alfred.reminders.tables import Reminder, ReminderRecipient
from alfred.turns.tables import QueueItem

metadata = Base.metadata

# Columns that name a user: every user name some row refers to is in one.
USER_NAME_COLUMNS = (
    Conversation.user_name,
    QueueItem.user_name,
    Attachment.user_name,
    Reminder.created_by,
    ReminderRecipient.user_name,
)
