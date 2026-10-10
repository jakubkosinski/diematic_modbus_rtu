from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from diematic.core.timestamp import compose_timestamp

WARSAW = ZoneInfo("Europe/Warsaw")
UTC = ZoneInfo("UTC")


def test_two_digit_years_are_read_as_20xx():
    moment = compose_timestamp(26, 7, 15, 10, 30, UTC)
    assert (moment.year, moment.month, moment.day, moment.hour, moment.minute) == (
        2026,
        7,
        15,
        10,
        30,
    )


def test_the_offset_follows_daylight_saving_time():
    summer = compose_timestamp(26, 7, 15, 10, 30, WARSAW)
    winter = compose_timestamp(26, 12, 1, 10, 30, WARSAW)
    assert summer.utcoffset() == timedelta(hours=2)
    assert winter.utcoffset() == timedelta(hours=1)
    assert summer.isoformat() == "2026-07-15T10:30:00+02:00"


@pytest.mark.parametrize(
    "parts",
    [
        (26, 2, 31, 10, 0),
        (26, 13, 1, 10, 0),
        (26, 0, 1, 10, 0),
        (26, 1, 1, 24, 0),
        (26, 1, 1, 0, 60),
    ],
)
def test_impossible_dates_give_none(parts):
    assert compose_timestamp(*parts, UTC) is None
