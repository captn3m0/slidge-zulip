"""End-to-end tests against a real Prosody, with Zulip faked.

Run via the harness that starts the server in the same shell:

    dev/with-prosody.sh uv run pytest -m integration

Zulip is faked rather than mocked loosely: the fake accepts exactly the request shape the
real API takes and hands back a real message id, because the id is what echo suppression
depends on. Everything on the XMPP side is genuine -- real Prosody, real TLS, real MUC,
real slixmpp -- since that is where the surprises have all been.
"""

import asyncio
import itertools
import queue
from pathlib import Path

import pytest
import zulip
from slixmpp import ClientXMPP

from bridgelib.identity import DEFAULT_XMPP_FIELD
from bridgelib.provenance import ProvenanceTracker

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge-xmpp"))
from zulip_xmpp_bridge import ZulipXmppBridge, build_identity  # noqa: E402

pytestmark = pytest.mark.integration

SERVER = ("127.0.0.1", 5222)
CA_CERT = Path(__file__).resolve().parent.parent / "dev/prosody/certs/localhost.crt"
ROOM = "bridgetest@conference.localhost"
OTHER_ROOM = "bridgetest2@conference.localhost"
STREAM, TOPIC = "general", "general-chat"
BOT_EMAIL = "bridge-relay-bot@521ab.zulipchat.com"
SITE = "https://521ab.zulipchat.com"


class FakeZulip:
    """Stands in for zulip.Client, recording sends and replaying an event queue."""

    def __init__(self):
        self.sent = []
        self.incoming = queue.Queue()
        self._ids = itertools.count(1000)
        self.email = BOT_EMAIL
        self.api_key = "botkey"
        self.profile_fields = [{"id": 1, "name": DEFAULT_XMPP_FIELD}]
        self.users = []

    def send_message(self, request):
        message_id = next(self._ids)
        self.sent.append(dict(request, id=message_id))
        return {"result": "success", "id": message_id}

    def call_on_each_event(self, callback, event_types=None, narrow=None, **kwargs):
        while True:
            event = self.incoming.get()
            if event is None:
                return
            callback(event)

    def call_endpoint(self, url, method="POST", request=None, **kwargs):
        assert url == "/realm/profile_fields"
        assert method == "GET"
        return {"custom_profile_fields": self.profile_fields}

    def get_users(self, request=None):
        assert request == {"include_custom_profile_fields": True}
        return {"members": self.users}

    def push(self, **message):
        base = {
            "id": next(self._ids),
            "type": "stream",
            "display_recipient": STREAM,
            "subject": TOPIC,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice Kapoor",
            "content": "<p>hi</p>",
        }
        base.update(message)
        self.incoming.put({"type": "message", "message": base})
        return base

    def stop(self):
        self.incoming.put(None)


class Probe(ClientXMPP):
    """A plain XMPP client standing in for a human in the room."""

    def __init__(self, jid, password, nick, room):
        super().__init__(jid, password)
        self.nick, self.room = nick, room
        self.register_plugin("xep_0030")
        self.register_plugin("xep_0045")
        self.joined = asyncio.Event()
        self.heard = []
        self.add_event_handler("session_start", self._start)
        self.add_event_handler("groupchat_message", self._on_msg)

    async def _start(self, _e):
        try:
            self.send_presence()
            await self.get_roster()
            await self.plugin["xep_0045"].join_muc_wait(self.room, self.nick)
            self.joined.set()
        except Exception as exc:  # noqa: BLE001
            print(f"probe {self.nick} join failed: {exc}")

    def _on_msg(self, msg):
        if msg["mucnick"] != self.nick and msg["body"]:
            self.heard.append((msg["mucnick"], msg["body"]))


def configure(client):
    client.ca_certs = CA_CERT
    client.enable_direct_tls = False
    return client


async def wait_for(predicate, timeout=15.0, interval=0.1):
    for _ in range(int(timeout / interval)):
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


@pytest.fixture
def fake_zulip(monkeypatch):
    fake = FakeZulip()
    # Every zulip.Client anywhere -- including the one identity.py builds for forging --
    # becomes this fake, so forged requests are observable.
    monkeypatch.setattr(zulip, "Client", lambda *a, **k: fake)
    return fake


async def start_bridge(fake, rooms, identity_config=None):
    xmpp_config = {
        "jid": "bridge@localhost",
        "password": "bridgepass",
        "nick": "zulip",
        "host": SERVER[0],
        "port": SERVER[1],
        "ca_certs": str(CA_CERT),
        "rooms": rooms,
    }
    zulip_config = {
        "email": BOT_EMAIL,
        "api_key": "botkey",
        "site": SITE,
        "stream": STREAM,
        "topic": TOPIC,
    }
    identity_config = identity_config or {"forge": "false"}
    identity = build_identity(identity_config, zulip_config, fake)
    bridge = ZulipXmppBridge(
        xmpp_config, zulip_config, identity, ProvenanceTracker(), fake
    )
    configure(bridge)
    bridge.connect(*SERVER)
    assert await wait_for(lambda: bridge._ready.is_set()), "bridge did not join its rooms"
    bridge.start_zulip_queue()
    return bridge, None


async def shutdown(bridge, task, fake, *probes):
    fake.stop()
    for probe in probes:
        probe.disconnect()
    bridge.disconnect()
    await asyncio.sleep(0.3)


@pytest.mark.asyncio
async def test_xmpp_message_reaches_zulip_with_code_intact(fake_zulip):
    bridge, task = await start_bridge(fake_zulip, [ROOM])
    probe = configure(Probe("alice@localhost", "alicepass", "alice", ROOM))
    probe.connect(*SERVER)
    assert await wait_for(probe.joined.is_set)

    probe.send_message(mto=ROOM, mbody="try `docker compose up -d`", mtype="groupchat")
    assert await wait_for(lambda: fake_zulip.sent), "nothing reached Zulip"

    sent = fake_zulip.sent[0]
    assert sent["type"] == "stream"
    assert sent["to"] == STREAM
    assert sent["subject"] == TOPIC
    # Unmapped sender: relayed by the bot with the name in the text, code preserved.
    assert sent["content"] == "**alice**: try `docker compose up -d`"
    await shutdown(bridge, task, fake_zulip, probe)


@pytest.mark.asyncio
async def test_xmpp_bold_is_translated_not_downgraded(fake_zulip):
    bridge, task = await start_bridge(fake_zulip, [ROOM])
    probe = configure(Probe("alice@localhost", "alicepass", "alice", ROOM))
    probe.connect(*SERVER)
    assert await wait_for(probe.joined.is_set)

    probe.send_message(mto=ROOM, mbody="*ship it*", mtype="groupchat")
    assert await wait_for(lambda: fake_zulip.sent)
    # XEP-0393 bold must become Zulip bold, not Zulip italic.
    assert fake_zulip.sent[0]["content"] == "**alice**: **ship it**"
    await shutdown(bridge, task, fake_zulip, probe)


@pytest.mark.asyncio
async def test_zulip_message_reaches_the_muc(fake_zulip):
    bridge, task = await start_bridge(fake_zulip, [ROOM])
    probe = configure(Probe("alice@localhost", "alicepass", "alice", ROOM))
    probe.connect(*SERVER)
    assert await wait_for(probe.joined.is_set)

    fake_zulip.push(content="<p>try <code>ls -la</code></p>")
    assert await wait_for(lambda: probe.heard), "nothing arrived in the MUC"

    nick, body = probe.heard[0]
    assert nick == "zulip"
    assert body == "*Alice Kapoor*: try `ls -la`"
    await shutdown(bridge, task, fake_zulip, probe)


@pytest.mark.asyncio
async def test_relayed_message_does_not_echo_back_to_its_origin(fake_zulip):
    """The bug that forging introduces, tested against a live server.

    A message from XMPP is posted to Zulip as that user, so the bot-sender check can no
    longer recognise it. Only provenance stops it coming straight back.
    """
    bridge, task = await start_bridge(fake_zulip, [ROOM])
    probe = configure(Probe("alice@localhost", "alicepass", "alice", ROOM))
    probe.connect(*SERVER)
    assert await wait_for(probe.joined.is_set)

    probe.send_message(mto=ROOM, mbody="hello", mtype="groupchat")
    assert await wait_for(lambda: fake_zulip.sent)
    relayed_id = fake_zulip.sent[0]["id"]

    # Zulip now reports that message back, as it genuinely would, attributed to a real
    # person rather than the bot.
    fake_zulip.push(id=relayed_id, sender_email="alice@example.com", content="<p>hello</p>")
    await asyncio.sleep(2.0)

    assert probe.heard == [], f"message echoed back to its origin room: {probe.heard}"
    await shutdown(bridge, task, fake_zulip, probe)


@pytest.mark.asyncio
async def test_message_fans_out_to_the_other_room(fake_zulip):
    """Signal -> Zulip -> Telegram, in miniature.

    The origin room is skipped but every other room must still receive it; that is what
    makes the spokes see each other.
    """
    bridge, task = await start_bridge(fake_zulip, [ROOM, OTHER_ROOM])
    talker = configure(Probe("alice@localhost", "alicepass", "alice", ROOM))
    listener = configure(Probe("bob@localhost", "bobpass", "bob", OTHER_ROOM))
    for probe in (talker, listener):
        probe.connect(*SERVER)
    assert await wait_for(lambda: talker.joined.is_set() and listener.joined.is_set())

    talker.send_message(mto=ROOM, mbody="from the first room", mtype="groupchat")
    assert await wait_for(lambda: fake_zulip.sent)
    relayed_id = fake_zulip.sent[0]["id"]

    fake_zulip.push(
        id=relayed_id, sender_email="alice@example.com", content="<p>from the first room</p>"
    )

    assert await wait_for(lambda: listener.heard), "did not reach the other room"
    assert talker.heard == [], "echoed back to the origin room"
    await shutdown(bridge, task, fake_zulip, talker, listener)


@pytest.mark.asyncio
async def test_linked_user_is_forged_into_zulip(fake_zulip):
    """The point of the whole project: a linked user posts as themselves."""
    fake_zulip.users = [
        {
            "email": "alice@example.com",
            "user_id": 42,
            "full_name": "Alice Kapoor",
            "profile_data": {"1": {"value": "alice@localhost"}},
        }
    ]

    bridge, task = await start_bridge(fake_zulip, [ROOM], identity_config={"forge": "true"})
    probe = configure(Probe("alice@localhost", "alicepass", "alice", ROOM))
    probe.connect(*SERVER)
    assert await wait_for(probe.joined.is_set)

    probe.send_message(mto=ROOM, mbody="posted as me", mtype="groupchat")
    assert await wait_for(lambda: fake_zulip.sent)

    sent = fake_zulip.sent[0]
    assert sent["forged"] == "true"
    assert sent["sender"] == "alice@example.com"
    # No "**alice**:" prefix -- the message is genuinely from her.
    assert sent["content"] == "posted as me"
    await shutdown(bridge, task, fake_zulip, probe)


@pytest.mark.asyncio
async def test_sender_key_is_the_real_jid_not_the_nickname(fake_zulip):
    """Identity must not be keyed on something a user can change at will.

    A nickname-based key would let anyone inherit someone else's Zulip identity simply by
    joining under their nick. The real JID is what slidge exposes for Signal and Telegram
    participants, and it is what the linking flow records.
    """
    fake_zulip.users = [
        {
            "email": "alice@example.com",
            "user_id": 42,
            "full_name": "Alice Kapoor",
            "profile_data": {"1": {"value": "alice@localhost"}},
        }
    ]
    bridge, task = await start_bridge(fake_zulip, [ROOM], identity_config={"forge": "true"})
    # Alice joins under a completely different nickname; identity must still resolve.
    probe = configure(Probe("alice@localhost", "alicepass", "not-alice-at-all", ROOM))
    probe.connect(*SERVER)
    assert await wait_for(probe.joined.is_set)

    probe.send_message(mto=ROOM, mbody="still me", mtype="groupchat")
    assert await wait_for(lambda: fake_zulip.sent)
    assert fake_zulip.sent[0]["sender"] == "alice@example.com"
    await shutdown(bridge, task, fake_zulip, probe)
