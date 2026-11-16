from __future__ import annotations

import pytest

from alfred.runtime.turns import notices
from tests.helpers import ADDRESSES, assert_addressed


@pytest.mark.parametrize("address", ADDRESSES)
def test_notice_ends_with_address_once(address: str) -> None:
    assert_addressed(notices.fell_back(address, "bonsai", "gemma"), address)


def test_fallback_names_both_brains() -> None:
    text = notices.fell_back("sir", "bonsai", "gemma")
    assert "bonsai" in text and "gemma" in text
