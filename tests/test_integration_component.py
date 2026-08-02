"""End-to-end check for the XEP-0114 component slots that Slidge gateways need.

Run via the same harness as the other live-XMPP tests:

    dev/with-prosody.sh uv run pytest -m integration
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from slixmpp import ClientXMPP
from slixmpp.componentxmpp import ComponentXMPP

pytestmark = pytest.mark.integration

SERVER = ("127.0.0.1", 5222)
COMPONENT = ("127.0.0.1", 5347)
CA_CERT = Path(__file__).resolve().parent.parent / "dev/prosody/certs/localhost.crt"
DOMAINS = ("telegram.localhost", "signal.localhost")
SECRET = "devsecret"


class DummySlidge(ComponentXMPP):
    def __init__(self, domain: str) -> None:
        super().__init__(domain, SECRET, *COMPONENT)
        self.domain = domain
        self.register_plugin("xep_0030")
        self.ready = asyncio.Event()
        self.add_event_handler("session_start", self._start)

    async def _start(self, _event):
        self.ready.set()


class Probe(ClientXMPP):
    def __init__(self) -> None:
        super().__init__("alice@localhost", "alicepass")
        self.register_plugin("xep_0030")
        self.ready = asyncio.Event()
        self.add_event_handler("session_start", self._start)

    async def _start(self, _event):
        self.send_presence()
        await self.get_roster()
        self.ready.set()


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.asyncio
async def test_prosody_accepts_slidge_gateway_component_slots(domain):
    component = DummySlidge(domain)
    probe = Probe()
    probe.ca_certs = CA_CERT
    probe.enable_direct_tls = False

    component.connect()
    probe.connect(*SERVER)

    try:
        await asyncio.wait_for(
            asyncio.gather(component.ready.wait(), probe.ready.wait()),
            10,
        )
        info = await asyncio.wait_for(probe.plugin["xep_0030"].get_info(jid=domain), 10)
    finally:
        probe.disconnect()
        component.disconnect()

    assert str(info["from"]) == domain
