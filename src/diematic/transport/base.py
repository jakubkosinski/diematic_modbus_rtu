from __future__ import annotations

from typing import Protocol


class Transport(Protocol):
    """Byte pipe to the RS485 bus. Implementations raise OSError subclasses on failure."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def read(self, max_wait: float) -> bytes:
        """Return received bytes, or b"" when nothing arrived within `max_wait` seconds.

        Raises ConnectionError when the peer closed the connection.
        """
        ...

    async def write(self, data: bytes) -> None: ...
