#!/usr/bin/env python3
"""Prove the local XMPP path works before building a bridge on top of it.

Two slixmpp clients join one MUC on the dev Prosody; one talks, the other must hear it.
That exercises the exact machinery the Zulip bridge will use -- connect, auth, join a
room, receive a groupchat message with a resolvable occupant -- and nothing else.

    prosody --config dev/prosody/prosody.cfg.lua &
    uv run python dev/smoke_xmpp.py
"""

import asyncio
import sys
from pathlib import Path

from slixmpp import ClientXMPP

SERVER = ("127.0.0.1", 5222)
ROOM = "general@conference.localhost"
TIMEOUT = 20
CA_CERT = Path(__file__).resolve().parent / "prosody" / "certs" / "localhost.crt"


class Occupant(ClientXMPP):
    def __init__(self, jid, password, nick):
        super().__init__(jid, password)
        self.nick = nick
        self.register_plugin("xep_0030")  # service discovery
        self.register_plugin("xep_0045")  # MUC
        self.register_plugin("xep_0199")  # ping
        self.joined = asyncio.Event()
        self.heard: list[tuple[str, str]] = []
        self.add_event_handler("session_start", self._start)
        self.add_event_handler("groupchat_message", self._on_groupchat)

    async def _start(self, _event):
        # slixmpp swallows exceptions raised inside event handlers, so anything that goes
        # wrong here would otherwise present as an unexplained timeout.
        try:
            self.send_presence()
            await self.get_roster()
            await self.plugin["xep_0045"].join_muc_wait(ROOM, self.nick)
        except Exception as exc:  # noqa: BLE001 - diagnostic
            print(f"{self.nick}: join failed: {type(exc).__name__}: {exc}")
            return
        print(f"{self.nick}: joined")
        self.joined.set()

    def _on_groupchat(self, msg):
        if msg["mucnick"] != self.nick:
            self.heard.append((msg["mucnick"], msg["body"]))


async def main() -> int:
    listener = Occupant("bridge@localhost", "bridgepass", "bridge")
    talker = Occupant("alice@localhost", "alicepass", "alice")

    for client in (listener, talker):
        # Trust the dev cert explicitly rather than disabling verification. Suppressing
        # the ssl_invalid_chain event stops slixmpp disconnecting, but leaves the stream
        # in a state where authentication silently never happens and the join just hangs
        # with no error -- so "ignore the bad cert" is a trap, not a shortcut.
        client.ca_certs = CA_CERT
        # slixmpp 1.17's connect() takes only (host, port). It defaults to direct TLS,
        # but 5222 is the STARTTLS port; mismatching them surfaces as the memorably
        # unhelpful [SSL: WRONG_VERSION_NUMBER].
        client.enable_direct_tls = False
        client.connect(*SERVER)

    try:
        await asyncio.wait_for(
            asyncio.gather(listener.joined.wait(), talker.joined.wait()), TIMEOUT
        )
    except asyncio.TimeoutError:
        print("FAIL: clients did not join within %ss" % TIMEOUT)
        return 1

    print(f"both clients joined {ROOM}")

    talker.send_message(mto=ROOM, mbody="hello from `alice`", mtype="groupchat")

    for _ in range(TIMEOUT * 10):
        if listener.heard:
            break
        await asyncio.sleep(0.1)

    for client in (listener, talker):
        client.disconnect()

    if not listener.heard:
        print("FAIL: listener heard nothing")
        return 1

    nick, body = listener.heard[0]
    print(f"listener heard from {nick!r}: {body!r}")
    assert nick == "alice", nick
    assert body == "hello from `alice`", body
    print("OK: XMPP round-trip works")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
