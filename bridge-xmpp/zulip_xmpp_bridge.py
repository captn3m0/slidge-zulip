#!/usr/bin/env python3
"""Bridge Zulip's #general > general-chat to a set of XMPP MUCs.

XMPP is the hub. Signal arrives through slidgnal and Telegram through slidgram, each
presenting its group as an ordinary MUC, and XMPP people get a native MUC of their own.
This process joins all of them and maps every one to a single Zulip stream and topic.

Two things make it more than a relay:

*Identity.* A message from Signal is posted to Zulip **as that person's real Zulip user**,
via Zulip's mirroring API, so it carries their avatar and is searchable by sender. See
``bridgelib.identity``. People who have not linked an account still get through, attributed
to the relay bot with their name in the text.

*Fan-out without echo.* Every room maps to the same Zulip topic, so a message from Signal
must reach Telegram and the XMPP room but must not come back to Signal. Provenance is
tracked by Zulip message id and the origin room is skipped -- see ``bridgelib.provenance``.
That is also what carries Signal<->Telegram traffic, which routes through Zulip.

Threading: slixmpp owns the asyncio loop; the Zulip event queue is a blocking call living
in an executor thread. Everything crossing back into XMPP goes through
``loop.call_soon_threadsafe``, because slixmpp is not thread-safe.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import functools
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import zulip
from slixmpp import ClientXMPP
from slixmpp.jid import JID

from bridgelib.formatting import (
    xmpp_message_to_zulip_markdown,
    zulip_html_to_xmpp,
)
from bridgelib.identity import (
    DEFAULT_SIGNAL_FIELD,
    DEFAULT_TELEGRAM_FIELD,
    DEFAULT_XMPP_FIELD,
    IdentityConfigError,
    IdentityResolver,
)
from bridgelib.provenance import ProvenanceTracker

log = logging.getLogger("zulip-xmpp")

# Used when the sender has not linked a Zulip account. Zulip renders this as markdown.
UNMAPPED_TEMPLATE = "**{sender}**: {body}"
# Zulip -> XMPP. XMPP has no way to impersonate, so the name goes in the text. Bold is
# XEP-0393 styling, matching what bridgelib.formatting emits for the body.
TO_XMPP_TEMPLATE = "*{sender}*: {body}"


class BridgeConfigError(Exception):
    pass


class ZulipXmppBridge(ClientXMPP):
    def __init__(
        self,
        xmpp_config: Dict[str, Any],
        zulip_config: Dict[str, Any],
        identity: IdentityResolver,
        provenance: ProvenanceTracker,
        zulip_client: zulip.Client,
    ) -> None:
        super().__init__(xmpp_config["jid"], xmpp_config["password"])
        self.xmpp_config = xmpp_config
        self.zulip_config = zulip_config
        self.identity = identity
        self.provenance = provenance
        self.zulip_client = zulip_client

        self.nick: str = xmpp_config.get("nick", "zulip")
        self.rooms: List[str] = xmpp_config["rooms"]
        self.stream: str = zulip_config["stream"]
        self.topic: str = zulip_config["topic"]
        self.server_url: str = zulip_config["site"].rstrip("/")

        self.joined_rooms: Set[str] = set()
        self._ready = asyncio.Event()

        self.register_plugin("xep_0030")  # service discovery
        self.register_plugin("xep_0045")  # MUC
        self.register_plugin("xep_0199")  # ping
        self.register_plugin("xep_0071")  # XHTML-IM, for clients that send it

        self.add_event_handler("session_start", self._on_session_start)
        self.add_event_handler("groupchat_message", self._on_groupchat_message)

    # -- XMPP -> Zulip ------------------------------------------------------------

    async def _on_session_start(self, _event: Any) -> None:
        # slixmpp swallows exceptions raised in event handlers, so a failure here would
        # otherwise look like the bridge simply sitting there doing nothing.
        try:
            self.send_presence()
            await self.get_roster()
            for room in self.rooms:
                await self.plugin["xep_0045"].join_muc_wait(room, self.nick)
                self.joined_rooms.add(room)
                log.info("joined %s", room)
            self._ready.set()
        except Exception:
            log.exception("failed to join rooms; shutting down")
            self.disconnect()

    def _on_groupchat_message(self, msg: Any) -> None:
        room = msg["from"].bare
        if room not in self.joined_rooms:
            return
        # Our own relayed output. Without this the bridge would feed Zulip its own echo.
        if msg["mucnick"] == self.nick:
            return
        body = msg["body"]
        if not body:
            return
        # Subject changes and other room events arrive as groupchat messages too.
        if msg["subject"]:
            return
        asyncio.ensure_future(self._relay_to_zulip(room, msg))

    def sender_key(self, room: str, msg: Any) -> str:
        """Stable identity for the person who sent a MUC message.

        Prefer the occupant's real JID: slidge sets it to something durable
        (``<uuid>@signal.example`` for Signal), which survives nickname changes and is
        what the linking flow records. In a semi-anonymous room the real JID is hidden
        from non-moderators, so fall back to the occupant JID -- weaker, because it
        changes if the person renames, but better than dropping the message.
        """
        real = self.plugin["xep_0045"].get_jid_property(room, msg["mucnick"], "jid")
        if real:
            return JID(real).bare
        return f"{room}/{msg['mucnick']}"

    async def _relay_to_zulip(self, room: str, msg: Any) -> None:
        try:
            sender = msg["mucnick"]
            key = self.sender_key(room, msg)
            xhtml = None
            if msg["html"] and msg["html"]["body"]:
                xhtml = str(msg["html"]["body"])
            body = xmpp_message_to_zulip_markdown(msg["body"], xhtml)
            if not body:
                return

            resolved = self.identity.resolve(key, room=room, display_name=sender)
            request: Dict[str, Any] = {
                "type": "stream",
                "to": self.stream,
                "subject": self.topic,
                "content": body if resolved else UNMAPPED_TEMPLATE.format(
                    sender=sender, body=body
                ),
            }
            client = self.zulip_client
            if resolved is not None:
                client = resolved.client
                request.update(resolved.extra)

            result = await asyncio.get_running_loop().run_in_executor(
                None, client.send_message, request
            )

            if result.get("result") != "success" and resolved is not None:
                # A forged send fails per-message when the bot lacks can_forge_sender or
                # the target is not in the realm. Losing one person's messages entirely
                # would be worse than losing their attribution, so degrade and continue.
                log.error(
                    "sending as %s failed (%s); relaying as the bot instead",
                    resolved.mapping.zulip_email if resolved.mapping else "?",
                    result.get("msg"),
                )
                result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    self.zulip_client.send_message,
                    {
                        "type": "stream",
                        "to": self.stream,
                        "subject": self.topic,
                        "content": UNMAPPED_TEMPLATE.format(sender=sender, body=body),
                    },
                )

            if result.get("result") != "success":
                log.error("zulip rejected message from %s: %s", key, result.get("msg"))
                return

            message_id = result.get("id")
            if message_id is not None:
                self.provenance.record(message_id, room)
        except Exception:
            log.exception("failed relaying message from %s to Zulip", room)

    # -- Zulip -> XMPP ------------------------------------------------------------

    def _zulip_event(self, event: Dict[str, Any]) -> None:
        """Runs in the executor thread that owns the Zulip event queue."""
        if event.get("type") != "message":
            return
        try:
            self._handle_zulip_message(event["message"])
        except Exception:
            log.exception("failed handling Zulip event")

    def _handle_zulip_message(self, message: Dict[str, Any]) -> None:
        if message.get("type") != "stream":
            return
        if message.get("display_recipient") != self.stream:
            return
        if message.get("subject") != self.topic:
            return
        # Anything the relay bot itself sent is our own output; never send it back out.
        if message.get("sender_email") == self.zulip_config["email"]:
            return

        targets = list(self.joined_rooms)
        origin = self.provenance.origin_of(message["id"])
        if origin is not None:
            # Everyone in the room it came from has already seen it. The other rooms
            # have not -- this is what carries Signal <-> Telegram traffic.
            targets = [room for room in targets if room != origin]
        if not targets:
            return

        body = TO_XMPP_TEMPLATE.format(
            sender=message["sender_full_name"],
            body=zulip_html_to_xmpp(message["content"], self.server_url),
        )
        for room in targets:
            # slixmpp is not thread-safe; hop back onto its loop.
            self.loop.call_soon_threadsafe(
                functools.partial(
                    self.send_message, mto=room, mbody=body, mtype="groupchat"
                )
            )

    async def wait_ready(self, timeout: float = 60.0) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout)

    def start_zulip_queue(self) -> None:
        """Consume the Zulip event queue on a daemon thread.

        ``call_on_each_event`` blocks forever by design, so it must not be run in a
        pooled executor: ``ThreadPoolExecutor.__exit__`` joins its workers, and a worker
        parked in a long-poll never returns. The bridge would then hang on shutdown
        instead of exiting on SIGTERM. A daemon thread just goes away with the process.
        """
        thread = threading.Thread(
            target=self._zulip_queue_loop, name="zulip-queue", daemon=True
        )
        thread.start()
        self._zulip_thread = thread

    def _zulip_queue_loop(self) -> None:
        log.info("listening on the Zulip event queue")
        try:
            self.zulip_client.call_on_each_event(
                self._zulip_event,
                ["message"],
                [],
                # Ask Zulip to render markdown for us. Its dialect has extensions that no
                # generic renderer handles correctly, so the server is the only authority
                # on what a message actually says.
                apply_markdown=True,
            )
        except Exception:
            log.exception("Zulip event queue stopped")


# -- configuration ---------------------------------------------------------------


def read_configuration(path: str) -> Dict[str, Dict[str, Any]]:
    parser = configparser.ConfigParser()
    if not parser.read(path):
        raise BridgeConfigError(f"could not read config file {path!r}")

    for required in ("xmpp", "zulip"):
        if required not in parser:
            raise BridgeConfigError(f"missing [{required}] section")

    xmpp_section = dict(parser["xmpp"])
    for key in ("jid", "password", "rooms"):
        if not xmpp_section.get(key):
            raise BridgeConfigError(f"[xmpp] {key} is required")

    rooms = [r.strip() for r in xmpp_section["rooms"].replace("\n", ",").split(",")]
    rooms = [r for r in rooms if r]
    if not rooms:
        raise BridgeConfigError("[xmpp] rooms lists no MUCs")
    if len(set(rooms)) != len(rooms):
        # Would double-deliver every Zulip message into that room.
        raise BridgeConfigError("[xmpp] rooms contains duplicates")

    zulip_section = dict(parser["zulip"])
    for key in ("email", "api_key", "site", "stream", "topic"):
        if not zulip_section.get(key):
            raise BridgeConfigError(f"[zulip] {key} is required")

    identity_section = dict(parser["identity"]) if "identity" in parser else {}
    if "forge" in identity_section:
        value = identity_section["forge"].strip().lower()
        if value not in {"true", "false"}:
            raise BridgeConfigError("[identity] forge must be true or false")

    return {
        "xmpp": {
            "jid": xmpp_section["jid"],
            "password": xmpp_section["password"],
            "nick": xmpp_section.get("nick", "zulip"),
            "host": xmpp_section.get("host") or None,
            "port": int(xmpp_section.get("port", 5222)),
            "ca_certs": xmpp_section.get("ca_certs") or None,
            "rooms": rooms,
        },
        "zulip": zulip_section,
        "identity": identity_section,
    }


def build_identity(
    identity_config: Dict[str, Any], zulip_config: Dict[str, Any], bot: zulip.Client
) -> IdentityResolver:
    try:
        return IdentityResolver(
            forge=identity_config.get("forge", "false").strip().lower() == "true",
            site=zulip_config["site"],
            bot_client=bot,
            forge_email=zulip_config["email"],
            forge_api_key=zulip_config["api_key"],
            xmpp_field=identity_config.get("xmpp_field", DEFAULT_XMPP_FIELD),
            telegram_field=identity_config.get("telegram_field", DEFAULT_TELEGRAM_FIELD),
            signal_field=identity_config.get("signal_field", DEFAULT_SIGNAL_FIELD),
        )
    except IdentityConfigError as exc:
        raise BridgeConfigError(str(exc)) from exc


async def run(config: Dict[str, Dict[str, Any]]) -> None:
    xmpp_config = config["xmpp"]
    zulip_config = config["zulip"]

    zulip_client = zulip.Client(
        email=zulip_config["email"],
        api_key=zulip_config["api_key"],
        site=zulip_config["site"],
    )
    identity = build_identity(config["identity"], zulip_config, zulip_client)
    log.info("%s", identity.describe())

    bridge = ZulipXmppBridge(
        xmpp_config, zulip_config, identity, ProvenanceTracker(), zulip_client
    )
    if xmpp_config["ca_certs"]:
        bridge.ca_certs = Path(xmpp_config["ca_certs"])
    # Port 5222 is STARTTLS. slixmpp defaults to direct TLS, which fails there with the
    # thoroughly unhelpful [SSL: WRONG_VERSION_NUMBER].
    bridge.enable_direct_tls = False

    bridge.connect(xmpp_config["host"], xmpp_config["port"])
    await bridge.wait_ready()
    bridge.start_zulip_queue()
    await bridge.disconnected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-c", "--config", required=True, help="path to bridge.conf")
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    try:
        config = read_configuration(args.config)
    except BridgeConfigError as exc:
        sys.exit(f"config error: {exc}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)
    try:
        loop.run_until_complete(run(config))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
