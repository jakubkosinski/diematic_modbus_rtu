"""Runs long-lived tasks and restarts them when they fail, so one fault never stops the app."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from diematic.transport.backoff import Backoff

logger = logging.getLogger(__name__)

_STABLE_AFTER = 30.0


async def supervise(
    name: str,
    factory: Callable[[], Awaitable[None]],
    *,
    backoff: Backoff | None = None,
    stable_after: float = _STABLE_AFTER,
) -> None:
    """Run `factory()` forever, restarting it with backoff after a crash or an early return."""
    backoff = backoff or Backoff()
    while True:
        started = time.monotonic()
        try:
            await factory()
            logger.warning("%s stopped unexpectedly, restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s crashed, restarting", name)
        if time.monotonic() - started >= stable_after:
            backoff.reset()
        await asyncio.sleep(backoff.next_delay())


class Supervisor:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []

    def start(self, name: str, factory: Callable[[], Awaitable[None]]) -> asyncio.Task[None]:
        task = asyncio.create_task(supervise(name, factory), name=name)
        self._tasks.append(task)
        return task

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
