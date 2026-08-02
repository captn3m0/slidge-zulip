#!/usr/bin/env python3
"""Prove Prosody's external-component slots are ready for Slidge gateways.

This does not start Slidge itself. It connects tiny XEP-0114 components at
``telegram.localhost`` and ``signal.localhost`` using the same host and secret Prosody is
configured to expect, then asks a normal XMPP client to do service discovery against
them.

If this passes, the missing pieces are Slidge-specific config and credentials rather than
Prosody's component plumbing.

    dev/with-prosody.sh uv run python dev/smoke_component.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from slixmpp import ClientXMPP
from slixmpp.componentxmpp import ComponentXMPP

SERVER = ("127.0.0.1", 5222)
COMPONENT = ("127.0.0.1", 5347)
DOMAINS = ("telegram.localhost", "signal.localhost")
SECRET = "devsecret"
TIMEOUT = 20
CA_CERT = Path(__file__).resolve().parent / "prosody" / "certs" / "localhost.crt"


class DummySlidge(ComponentXMPP):
    def __init__(self, domain: str) -> None:
        super().__init__(domain, SECRET, *COMPONENT)
        self.domain = domain
        self.register_plugin("xep_0030")
        self.ready = asyncio.Event()
        self.add_event_handler("session_start", self._start)

    async def _start(self, _event):
        print(f"component authenticated as {self.domain}")
        self.ready.set()


class Probe(ClientXMPP):
    def __init__(self) -> None:
        super().__init__("alice@localhost", "alicepass")
        self.register_plugin("xep_0030")
        self.ready = asyncio.Event()
        self.add_event_handler("session_start", self._start)

    async def _start(self, _event):
        try:
            self.send_presence()
            await self.get_roster()
        except Exception as exc:  # noqa: BLE001 - diagnostic
            print(f"probe login failed: {type(exc).__name__}: {exc}")
            return
        self.ready.set()


async def main() -> int:
    components = [DummySlidge(domain) for domain in DOMAINS]
    probe = Probe()

    probe.ca_certs = CA_CERT
    probe.enable_direct_tls = False

    for component in components:
        component.connect()
    probe.connect(*SERVER)

    try:
        await asyncio.wait_for(
            asyncio.gather(
                probe.ready.wait(), *(component.ready.wait() for component in components)
            ),
            TIMEOUT,
        )
        infos = []
        for domain in DOMAINS:
            info = await asyncio.wait_for(
                probe.plugin["xep_0030"].get_info(jid=domain),
                TIMEOUT,
            )
            infos.append(info)
    except asyncio.TimeoutError:
        print(f"FAIL: component handshake or disco query timed out within {TIMEOUT}s")
        return 1
    finally:
        probe.disconnect()
        for component in components:
            component.disconnect()

    for info in infos:
        print(f"probe reached {info['from']}")
    print("OK: Prosody accepts external components for Telegram and Signal")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
