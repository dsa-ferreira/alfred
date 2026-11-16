from __future__ import annotations

from collections.abc import Callable

import pytest

from alfred.features.attachments import notices
from tests.helpers import ADDRESSES, assert_addressed

NOTICES: dict[str, Callable[[str], str]] = {
    "too_large": lambda a: notices.too_large(a, 20_000_000),
    "unreadable": notices.unreadable,
    "unreadable_pdf": lambda a: notices.unreadable(a, "PDF"),
    "not_kept": notices.not_kept,
}


@pytest.mark.parametrize("address", ADDRESSES)
@pytest.mark.parametrize("name", list(NOTICES))
def test_notice_ends_with_address_once(name: str, address: str) -> None:
    assert_addressed(NOTICES[name](address), address)


def test_attachment_notices_say_why() -> None:
    assert "20 MB" in notices.too_large("sir", 20_000_000)
    assert "2.5 MB" in notices.too_large("sir", 2_500_000)
    assert "can't open that PDF" in notices.unreadable("Mr. Ferreira", "PDF")
    assert "send it again" in notices.not_kept("sir")
