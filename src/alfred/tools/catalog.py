"""Every tool alfred offers itself, by model-facing name.

Used to check allow-lists at startup: an exact ``alfred_*`` entry must name
one of these (change add-multiple-users, ``users`` spec).
"""

from __future__ import annotations


def alfred_tool_names() -> frozenset[str]:
    # Imported here, not at the top: these modules import alfred.config,
    # whose load_config calls this.
    from alfred.attachments.tools import READ_DOCUMENT, VIEW_ATTACHMENT
    from alfred.memory.facts import NAMES as FACT_NAMES
    from alfred.memory.search import RECALL_CONVERSATION, SEARCH_CONVERSATIONS
    from alfred.reminders.tools import NAMES as REMINDER_NAMES
    from alfred.tools.brains import SET_MODEL
    from alfred.tools.calculator import CALCULATE
    from alfred.tools.conversation import END_CONVERSATION
    from alfred.tools.dates import DATE
    from alfred.web.tools import NAMES as WEB_NAMES

    return frozenset(
        {
            READ_DOCUMENT,
            VIEW_ATTACHMENT,
            RECALL_CONVERSATION,
            SEARCH_CONVERSATIONS,
            SET_MODEL,
            CALCULATE,
            END_CONVERSATION,
            DATE,
            *FACT_NAMES,
            *REMINDER_NAMES,
            *WEB_NAMES,
        }
    )
