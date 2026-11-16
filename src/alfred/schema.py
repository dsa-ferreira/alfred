"""The whole schema: every slice's tables on one metadata, for Alembic and startup.

A slice defines its tables in its own ``tables.py``; importing it here
registers them on :data:`metadata`.
"""

from __future__ import annotations

from alfred.features.archive import tables as _archive  # noqa: F401  (registers its tables)
from alfred.features.attachments.tables import Attachment
from alfred.features.conversations.tables import Conversation
from alfred.features.facts import tables as _facts  # noqa: F401  (registers its tables)
from alfred.features.reminders.tables import Reminder, ReminderRecipient
from alfred.infra.db.base import Base
from alfred.runtime.turns.tables import QueueItem

metadata = Base.metadata

# Columns that name a user: every user name some row refers to is in one.
USER_NAME_COLUMNS = (
    Conversation.user_name,
    QueueItem.user_name,
    Attachment.user_name,
    Reminder.created_by,
    ReminderRecipient.user_name,
)
