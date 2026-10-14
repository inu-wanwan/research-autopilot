"""Relay 127.0.0.1:23119 inside WSL to the Windows Zotero app (docs/operations/eagle_wsl_setup.md, Zotero).

WSL NAT cannot reach Windows' loopback, where Zotero listens.  Windows forwards <WSL host address>:23120 to
127.0.0.1:23119 (netsh portproxy, firewall-scoped to the WSL interface); this relay makes the usual
http://127.0.0.1:23119 work inside WSL for the Zotero Connector (Playwright Chromium) and the Local API.
The Windows host address is looked up per connection (it changes across reboots).  Standard library only.
Usage: python infra/host/zotero_bridge.py [--listen 127.0.0.1:23119] [--port 23120]
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import struct
import sys
from typing import Callable

ROUTE_TABLE = "/proc/net/route"
RTF_GATEWAY = 0x2


def default_gateway(route_table: str = ROUTE_TABLE) -> str:
    """IPv4 default gateway of this WSL instance (= the Windows host on the WSL virtual switch)."""
    with open(route_table) as f:
        next(f, None)  # header
        for line in f:
            fields = line.split()
            # Iface Destination Gateway Flags ...; hex fields are little-endian
            if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & RTF_GATEWAY:
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    raise RuntimeError("no IPv4 default route")


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy until EOF, then half-close the other side (an HTTP client may shut down its write side first)."""
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        writer.close()


def handler(port: int, gateway: Callable[[], str] = default_gateway):
    async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(gateway(), port), timeout=5)
        except (OSError, asyncio.TimeoutError, RuntimeError):
            client_w.close()  # Zotero not running / bridge not set up: the caller sees the connection closed
            return
        try:
            await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))
        finally:
            for w in (up_w, client_w):
                w.close()
    return handle


async def serve(listen_host: str, listen_port: int, port: int) -> None:
    server = await asyncio.start_server(handler(port), listen_host, listen_port)
    print(f"zotero bridge: {listen_host}:{listen_port} -> <default gateway>:{port}", file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--listen", default="127.0.0.1:23119")
    ap.add_argument("--port", type=int, default=23120, help="port of the Windows portproxy")
    a = ap.parse_args()
    host, _, lport = a.listen.rpartition(":")
    asyncio.run(serve(host or "127.0.0.1", int(lport), a.port))


if __name__ == "__main__":
    main()
