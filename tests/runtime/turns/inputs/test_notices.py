from __future__ import annotations

import pytest

from alfred.runtime.turns.inputs import notices
from tests.helpers import ADDRESSES, assert_addressed


@pytest.mark.parametrize("address", ADDRESSES)
@pytest.mark.parametrize(
    ("tool", "reason"),
    [("mcparr_add_media", "new_message"), ("mcparr_add_media", "expired"), ("x", "not_delivered")],
)
def test_notice_ends_with_address_once(tool: str, reason: str, address: str) -> None:
    assert_addressed(notices.input_not_done(address, tool, reason), address)


@pytest.mark.parametrize(
    ("reason", "why"),
    [
        ("new_message", "you sent a new message before answering my question"),
        ("expired", "my question went unanswered for too long"),
        ("not_delivered", "I could not send you my question"),
    ],
)
def test_input_not_done_says_what_and_why(reason: str, why: str) -> None:
    assert notices.input_not_done("madam", "mcparr_add_media", reason) == (
        f"Nothing was done: I did not run mcparr_add_media, as {why}, madam."
    )
