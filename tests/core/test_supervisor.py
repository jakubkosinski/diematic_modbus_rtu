import asyncio
import contextlib
import itertools

from diematic.core.supervisor import Supervisor, supervise
from diematic.transport.backoff import Backoff


def quick():
    return Backoff(initial=0.01, maximum=0.02, jitter=0)


async def run_for(coro, seconds):
    task = asyncio.create_task(coro)
    await asyncio.sleep(seconds)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_crashing_task_is_restarted():
    calls = []

    async def flaky():
        calls.append(1)
        raise RuntimeError("boom")

    await run_for(supervise("flaky", flaky, backoff=quick()), 0.2)
    assert len(calls) >= 3


async def test_task_that_returns_is_restarted():
    calls = []

    async def finishes():
        calls.append(1)

    await run_for(supervise("finishes", finishes, backoff=quick()), 0.2)
    assert len(calls) >= 3


async def test_exceptions_never_escape_the_supervisor():
    async def broken():
        raise ValueError("nope")

    task = asyncio.create_task(supervise("broken", broken, backoff=quick()))
    await asyncio.sleep(0.1)
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_cancellation_stops_a_healthy_task():
    cancelled = asyncio.Event()

    async def healthy():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    await run_for(supervise("healthy", healthy, backoff=quick()), 0.05)
    assert cancelled.is_set()


async def test_backoff_grows_for_a_task_that_keeps_failing():
    stamps = []

    async def failing():
        stamps.append(asyncio.get_running_loop().time())
        raise RuntimeError

    backoff = Backoff(initial=0.02, maximum=0.2, factor=2, jitter=0)
    await run_for(supervise("failing", failing, backoff=backoff), 0.5)
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert gaps[1] > gaps[0]


async def test_backoff_resets_after_a_stable_run():
    stamps = []

    async def sometimes():
        stamps.append(asyncio.get_running_loop().time())
        if len(stamps) == 2:
            await asyncio.sleep(0.05)
        raise RuntimeError

    backoff = Backoff(initial=0.02, maximum=1.0, factor=4, jitter=0)
    await run_for(supervise("sometimes", sometimes, backoff=backoff, stable_after=0.04), 0.4)
    assert stamps[2] - stamps[1] < 0.15


async def test_supervisor_starts_and_stops_tasks():
    started = asyncio.Event()

    async def worker():
        started.set()
        await asyncio.sleep(10)

    supervisor = Supervisor()
    supervisor.start("worker", worker)
    await asyncio.wait_for(started.wait(), 1)
    await supervisor.stop()
    await supervisor.stop()
