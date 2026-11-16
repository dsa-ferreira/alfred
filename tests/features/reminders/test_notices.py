from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from alfred.features.reminders import notices
from tests.helpers import ADDRESSES, assert_addressed

LISBON = ZoneInfo("Europe/Lisbon")
DUE = datetime(2026, 11, 9, 17, 0, tzinfo=LISBON)
TODAY = date(2026, 11, 9)


@pytest.mark.parametrize("address", ADDRESSES)
def test_notice_ends_with_address_once(address: str) -> None:
    assert_addressed(notices.reminder(address, "call mum", DUE, TODAY), address)


@pytest.mark.parametrize("address", ADDRESSES)
def test_reminder_notice(address: str) -> None:
    text = notices.reminder(address, "call mum", DUE, TODAY)
    assert text == (
        f'Your reminder, due at 17:00: "call mum". My apologies for the plain delivery, {address}.'
    )


def test_reminder_notice_on_another_day() -> None:
    text = notices.reminder("sir", "call mum", DUE, date(2026, 11, 12))
    assert 'due at 17:00 on Monday 9 November: "call mum"' in text
