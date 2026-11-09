from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from alfred import notices

LISBON = ZoneInfo("Europe/Lisbon")
DUE = datetime(2026, 11, 9, 17, 0, tzinfo=LISBON)
TODAY = date(2026, 11, 9)

NOTICES: dict[str, Callable[[str], str]] = {
    "text_only": notices.text_only,
    "failed": notices.failed,
    "failed_partial": lambda a: notices.failed(a, partial=True),
    "unreachable": notices.unreachable,
    "unreachable_mid": notices.unreachable_mid,
    "too_many_steps": notices.too_many_steps,
    "too_many_steps_partial": lambda a: notices.too_many_steps(a, partial=True),
    "fell_back": lambda a: notices.fell_back(a, "bonsai", "gemma"),
    "reminder": lambda a: notices.reminder(a, "call mum", DUE, TODAY),
}


@pytest.mark.parametrize("address", ["sir", "Mr. Ferreira"])
@pytest.mark.parametrize("name", list(NOTICES))
def test_notice_ends_with_address_once(name: str, address: str) -> None:
    text = NOTICES[name](address)
    assert text.removesuffix(notices.PARTIAL).endswith(f", {address}.")
    assert text.count(address) == 1


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
    assert NOTICES[name]("sir").endswith(notices.PARTIAL) == partial


def test_partial_carries_no_address() -> None:
    assert "sir" not in notices.PARTIAL


def test_fallback_names_both_brains() -> None:
    text = notices.fell_back("sir", "bonsai", "gemma")
    assert "bonsai" in text and "gemma" in text


@pytest.mark.parametrize("address", ["sir", "Mr. Ferreira"])
def test_reminder_notice(address: str) -> None:
    text = notices.reminder(address, "call mum", DUE, TODAY)
    assert text == (
        f'Your reminder, due at 17:00: "call mum". My apologies for the plain delivery, {address}.'
    )


def test_reminder_notice_on_another_day() -> None:
    text = notices.reminder("sir", "call mum", DUE, date(2026, 11, 12))
    assert 'due at 17:00 on Monday 9 November: "call mum"' in text
