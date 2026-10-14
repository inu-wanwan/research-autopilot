"""infra/host/zotero_bridge.py: gateway lookup and a real loopback relay round-trip (Eagle Zotero bridge)."""

import asyncio
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "zotero_bridge", Path(__file__).resolve().parents[1] / "infra" / "host" / "zotero_bridge.py")
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
# eth0 172.29.96.0/20 link route, then the default route via 172.29.96.1 (hex fields are little-endian)
LINK = "eth0\t0060001D\t00000000\t0001\t0\t0\t0\t00F0FFFF\t0\t0\t0\n"
DEFAULT = "eth0\t00000000\t01601DAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"


def test_default_gateway_from_route_table(tmp_path):
    rt = tmp_path / "route"
    rt.write_text(HEADER + LINK + DEFAULT)
    assert bridge.default_gateway(str(rt)) == "172.29.96.1"


def test_default_gateway_missing(tmp_path):
    rt = tmp_path / "route"
    # link route only; a destination-0 route without RTF_GATEWAY must not match either
    rt.write_text(HEADER + LINK + "eth1\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n")
    with pytest.raises(RuntimeError, match="no IPv4 default route"):
        bridge.default_gateway(str(rt))
    (tmp_path / "empty").write_text("")
    with pytest.raises(RuntimeError):
        bridge.default_gateway(str(tmp_path / "empty"))


async def _start(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_relay_round_trip_and_half_close():
    async def scenario():
        seen = []

        async def upstream(r, w):  # echoes until the client half-closes, then replies with a trailer
            while data := await r.read(65536):
                seen.append(data)
                w.write(data.upper())
                await w.drain()
            w.write(b"|BYE")
            await w.drain()
            w.close()

        up, up_port = await _start(upstream)
        relay, relay_port = await _start(bridge.handler(up_port, gateway=lambda: "127.0.0.1"))
        async with up, relay:
            r, w = await asyncio.open_connection("127.0.0.1", relay_port)
            req = b"GET /connector/ping HTTP/1.1\r\n\r\n"
            w.write(req)
            await w.drain()
            assert await asyncio.wait_for(r.readexactly(len(req)), 2) == req.upper()
            big = b"x" * 300_000  # larger than one read chunk
            w.write(big)
            w.write_eof()  # half-close: the response must still come back
            rest = await asyncio.wait_for(r.read(), 5)
            w.close()
            assert rest == big.upper() + b"|BYE"
            assert b"".join(seen) == req + big

    asyncio.run(scenario())


def test_refused_upstream_closes_client():
    async def scenario():
        dead, dead_port = await _start(lambda r, w: w.close())
        dead.close()
        await dead.wait_closed()  # nothing listens on dead_port now -> connection refused
        relay, relay_port = await _start(bridge.handler(dead_port, gateway=lambda: "127.0.0.1"))
        async with relay:
            r, w = await asyncio.open_connection("127.0.0.1", relay_port)
            assert await asyncio.wait_for(r.read(), 5) == b""
            w.close()

    asyncio.run(scenario())


def test_no_gateway_closes_client():
    def no_route():
        raise RuntimeError("no IPv4 default route")

    async def scenario():
        relay, relay_port = await _start(bridge.handler(1, gateway=no_route))
        async with relay:
            r, w = await asyncio.open_connection("127.0.0.1", relay_port)
            assert await asyncio.wait_for(r.read(), 5) == b""
            w.close()

    asyncio.run(scenario())
