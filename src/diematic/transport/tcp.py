from __future__ import annotations

import asyncio
import contextlib
import socket

READ_CHUNK = 1024


class TcpTransport:
    """Raw RTU frames over a TCP socket, as exposed by RS485-to-Ethernet converters."""

    def __init__(self, host: str, port: int, connect_timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self._connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def is_open(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def open(self) -> None:
        await self.close()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self._connect_timeout
        )
        self._reader, self._writer = reader, writer
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    async def close(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    async def read(self, max_wait: float) -> bytes:
        if self._reader is None:
            raise ConnectionError("transport is not open")
        try:
            data = await asyncio.wait_for(self._reader.read(READ_CHUNK), max_wait)
        except TimeoutError:
            return b""
        if not data:
            raise ConnectionError("connection closed by peer")
        return data

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError("transport is not open")
        self._writer.write(data)
        await self._writer.drain()
