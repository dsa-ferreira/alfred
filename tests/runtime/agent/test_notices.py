from __future__ import annotations

from collections.abc import Callable

import pytest

from alfred.runtime.agent import notices
from tests.helpers import ADDRESSES, assert_addressed

NOTICES: dict[str, Callable[[str], str]] = {
    "failed": notices.failed,
    "failed_partial": lambda a: notices.failed(a, partial=True),
    "unreachable": notices.unreachable,
    "unreachable_mid": notices.unreachable_mid,
    "too_many_steps": notices.too_many_steps,
    "too_many_steps_partial": lambda a: notices.too_many_steps(a, partial=True),
}


@pytest.mark.parametrize("address", ADDRESSES)
@pytest.mark.parametrize("name", list(NOTICES))
def test_notice_ends_with_address_once(name: str, address: str) -> None:
    assert_addressed(NOTICES[name](address), address, after=notices._PARTIAL)


@pytest.mark.parametrize(
    ("name", "partial"),
    [
        ("failed", False),
        ("failed_partial", True),
        ("unreachable", False),
        ("unreachable_mid", True),
        ("too_many_steps", False),
        ("too_many_steps_partial", True),
    ],
)
def test_partial_suffix(name: str, partial: bool) -> None:
    assert NOTICES[name]("sir").endswith(notices._PARTIAL) == partial


def test_partial_carries_no_address() -> None:
    assert "sir" not in notices._PARTIAL
