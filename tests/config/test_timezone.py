import os

import pytest

from diematic.config import timezone as tz
from diematic.config.timezone import resolve_timezone


@pytest.fixture
def system(tmp_path):
    """A fake machine: an /etc/localtime symlink and an /etc/timezone file under tmp_path."""

    class System:
        localtime = tmp_path / "localtime"
        timezone_file = tmp_path / "timezone"

        def link(self, name):
            self.localtime.symlink_to(f"/usr/share/zoneinfo/{name}")

        def write(self, text):
            self.timezone_file.write_text(text, encoding="utf-8")

    return System()


def resolve(system, configured=None, **environment):
    return resolve_timezone(configured, environment, system.localtime, system.timezone_file)


def test_the_configured_zone_wins_over_everything(system):
    system.link("Asia/Tokyo")
    result = resolve(system, "Europe/Warsaw", TZ="America/New_York")
    assert (result.name, result.source) == ("Europe/Warsaw", tz.CONFIGURED)


def test_the_tz_variable_is_used_when_nothing_is_configured(system):
    system.link("Asia/Tokyo")
    result = resolve(system, TZ="Europe/Warsaw")
    assert (result.name, result.source) == ("Europe/Warsaw", tz.ENVIRONMENT)


def test_a_posix_style_leading_colon_in_tz_is_accepted(system):
    assert resolve(system, TZ=":Europe/Warsaw").name == "Europe/Warsaw"


def test_the_system_zone_is_read_from_the_localtime_link(system):
    system.link("Europe/Warsaw")
    result = resolve(system)
    assert (result.name, result.source) == ("Europe/Warsaw", tz.SYSTEM)


def test_the_system_zone_is_read_from_etc_timezone_when_there_is_no_link(system):
    system.write("Europe/Warsaw\n")
    assert resolve(system).name == "Europe/Warsaw"


def test_the_link_is_preferred_to_the_timezone_file(system):
    system.link("Europe/Warsaw")
    system.write("Asia/Tokyo")
    assert resolve(system).name == "Europe/Warsaw"


def test_utc_is_the_last_resort_and_is_marked_as_a_fallback(system):
    result = resolve(system)
    assert (result.name, result.source, result.is_fallback) == ("UTC", tz.DEFAULT, True)


@pytest.mark.parametrize("bad", ["", "Mars/Olympus", "not a zone", "../../etc/passwd"])
def test_unusable_values_are_skipped(system, bad):
    system.link("Europe/Warsaw")
    result = resolve(system, TZ=bad)
    assert (result.name, result.source) == ("Europe/Warsaw", tz.SYSTEM)


def test_garbage_in_the_system_files_is_ignored(system):
    system.write("???\n")
    assert resolve(system).is_fallback


def test_a_resolved_zone_is_not_a_fallback(system):
    assert not resolve(system, TZ="Europe/Warsaw").is_fallback


def test_the_real_environment_is_used_by_default(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    assert resolve_timezone().name == "Europe/Warsaw"
    assert os.environ["TZ"] == "Europe/Warsaw"
