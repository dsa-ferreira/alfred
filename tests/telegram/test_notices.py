from __future__ import annotations

import pytest

from alfred.telegram import notices
from tests.helpers import ADDRESSES, assert_addressed


@pytest.mark.parametrize("address", ADDRESSES)
@pytest.mark.parametrize("notice", [notices.text_only, notices.unsupported])
def test_notice_ends_with_address_once(notice, address: str) -> None:  # type: ignore[no-untyped-def]
    assert_addressed(notice(address), address)


def test_unsupported_says_what_alfred_reads() -> None:
    assert notices.unsupported("sir") == "I can read text, images and PDFs, sir."
