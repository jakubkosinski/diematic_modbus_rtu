import asyncio
import contextlib
import os
import time

from diematic.core.heartbeat import heartbeat_age, run_heartbeat


def test_age_is_none_without_a_heartbeat(tmp_path):
    assert heartbeat_age(tmp_path / "missing") is None


def test_age_grows_with_the_file_modification_time(tmp_path):
    path = tmp_path / "beat"
    path.write_text("x")
    os.utime(path, (time.time() - 120, time.time() - 120))
    assert 119 < heartbeat_age(path) < 125


async def test_heartbeat_is_refreshed_periodically(tmp_path):
    path = tmp_path / "beat"
    task = asyncio.create_task(run_heartbeat(path, interval=0.05))
    await asyncio.sleep(0.02)
    os.utime(path, (time.time() - 100, time.time() - 100))
    await asyncio.sleep(0.15)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert heartbeat_age(path) < 5


async def test_unwritable_location_does_not_stop_the_heartbeat(tmp_path):
    task = asyncio.create_task(run_heartbeat(tmp_path / "no-such-dir" / "beat", interval=0.02))
    await asyncio.sleep(0.1)
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
