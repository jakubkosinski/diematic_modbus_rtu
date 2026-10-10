"""Liveness file for container health checks: it goes stale only if the event loop hangs."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def heartbeat_age(path: Path) -> float | None:
    """Seconds since the heartbeat was last written, or None if there is none."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


async def run_heartbeat(path: Path, interval: float = 10.0) -> None:
    while True:
        try:
            await asyncio.to_thread(path.write_text, f"{time.time():.0f}\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("cannot write the heartbeat file: %s", exc.strerror)
        await asyncio.sleep(interval)
