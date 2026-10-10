import asyncio

import pytest

from diematic.transport.tcp import TcpTransport


@pytest.fixture
async def echo_server():
    received = bytearray()
    connections = []

    async def handle(reader, writer):
        connections.append(writer)
        while data := await reader.read(100):
            received.extend(data)
            writer.write(data.upper())
            await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield port, received, connections
    server.close()
    for writer in connections:
        writer.close()
    await server.wait_closed()


async def test_round_trip(echo_server):
    port, received, _ = echo_server
    transport = TcpTransport("127.0.0.1", port)
    await transport.open()
    assert transport.is_open
    await transport.write(b"abc")
    assert await transport.read(max_wait=1) == b"ABC"
    assert bytes(received) == b"abc"
    await transport.close()
    assert not transport.is_open


async def test_read_returns_empty_bytes_on_timeout(echo_server):
    port, _, _ = echo_server
    transport = TcpTransport("127.0.0.1", port)
    await transport.open()
    assert await transport.read(max_wait=0.05) == b""
    await transport.close()


async def test_read_raises_when_peer_closes(echo_server):
    port, _, connections = echo_server
    transport = TcpTransport("127.0.0.1", port)
    await transport.open()
    await transport.write(b"x")
    await transport.read(max_wait=1)
    connections[0].close()
    with pytest.raises(ConnectionError):
        await transport.read(max_wait=1)
    await transport.close()


async def test_io_before_open_raises_connection_error():
    transport = TcpTransport("127.0.0.1", 1)
    with pytest.raises(ConnectionError):
        await transport.read(max_wait=0.01)
    with pytest.raises(ConnectionError):
        await transport.write(b"x")


async def test_open_fails_with_oserror_when_nothing_listens():
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    with pytest.raises(OSError):
        await TcpTransport("127.0.0.1", port, connect_timeout=1).open()


async def test_close_is_idempotent_and_reopen_works(echo_server):
    port, _, _ = echo_server
    transport = TcpTransport("127.0.0.1", port)
    await transport.close()
    await transport.open()
    await transport.open()
    await transport.write(b"q")
    assert await transport.read(max_wait=1) == b"Q"
    await transport.close()
    await transport.close()
