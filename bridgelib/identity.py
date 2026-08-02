"""Sender identity resolution for the Zulip side of the bridge.

The bridge has two behaviours:

``forge = false``
    No impersonation. Everyone relays through the bridge account with a visible name
    prefix in the message body.

``forge = true``
    The bridge account uses Zulip's mirroring API (``forged=true`` plus
    ``sender=<email>``) to post as real Zulip users. The sender is resolved by looking up
    values in Zulip custom profile fields rather than from a separate mapping database.

The custom profile fields default to the labels currently used in this realm:

- ``XMPP Username``
- ``Telegram Username``
- ``Signal Username``

Unmatched or ambiguous senders always fall back to the prefixed relay path. Losing
attribution is acceptable; dropping or mis-attributing a message is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

import zulip

# Zulip only accepts `forged`/`sender` from a request whose *client name* is one of its
# recognised mirrors -- see the `if client.name in ["irc_mirror", "jabber_mirror",
# "JabberMirror"]` branch in zerver/views/message_send.py. zulip.Client turns its
# `client=` argument into the User-Agent, which is where the server reads that name from.
MIRROR_CLIENT_NAME = "irc_mirror"

FORGE = "forge"
PREFIX = "prefix"

DEFAULT_XMPP_FIELD = "XMPP Username"
DEFAULT_TELEGRAM_FIELD = "Telegram Username"
DEFAULT_SIGNAL_FIELD = "Signal Username"

TELEGRAM_DOMAIN_HINT = "telegram."
SIGNAL_DOMAIN_HINT = "signal."

log = logging.getLogger(__name__)


class IdentityConfigError(Exception):
    pass


@dataclass(frozen=True)
class Mapping:
    external_id: str
    zulip_email: str
    zulip_user_id: Optional[int] = None
    zulip_full_name: Optional[str] = None


@dataclass
class ResolvedSender:
    """How to post to Zulip as one particular person."""

    client: zulip.Client
    extra: Dict[str, Any] = field(default_factory=dict)
    mapping: Optional[Mapping] = None


class IdentityResolver:
    def __init__(
        self,
        *,
        forge: bool,
        site: str,
        bot_client: zulip.Client,
        forge_email: str,
        forge_api_key: str,
        xmpp_field: str = DEFAULT_XMPP_FIELD,
        telegram_field: str = DEFAULT_TELEGRAM_FIELD,
        signal_field: str = DEFAULT_SIGNAL_FIELD,
    ) -> None:
        self.forge = forge
        self.site = site
        self.bot_client = bot_client
        self.xmpp_field = xmpp_field
        self.telegram_field = telegram_field
        self.signal_field = signal_field
        self._forge_client: Optional[zulip.Client] = None
        self._directory: Optional[Dict[str, Dict[str, Mapping | None]]] = None

        if self.forge:
            if not (forge_email and forge_api_key):
                raise IdentityConfigError("forge mode requires Zulip email and API key")
            self._forge_client = zulip.Client(
                email=forge_email,
                api_key=forge_api_key,
                site=site,
                client=MIRROR_CLIENT_NAME,
            )

    def resolve(
        self,
        external_id: str,
        *,
        room: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> Optional[ResolvedSender]:
        """Return how to post as this sender, or None to fall back to prefix mode."""
        if not self.forge:
            return None

        field_name = self._field_for(external_id, room)
        if field_name is None:
            return None

        directory = self._directory_for(field_name)
        for candidate in self._candidates(external_id, display_name):
            match = directory.get(candidate)
            if match is None:
                if candidate in directory:
                    log.warning("identity: %r is ambiguous in %s", candidate, field_name)
                continue
            assert self._forge_client is not None
            return ResolvedSender(
                client=self._forge_client,
                # `forged` is parsed as `forged_str in ["yes", "true"]` server-side, so it
                # must be the string, not a bool. `sender` must be an email: the mirroring
                # path explicitly rejects user IDs.
                extra={"forged": "true", "sender": match.zulip_email},
                mapping=match,
            )
        return None

    def describe(self) -> str:
        if not self.forge:
            return "identity: prefix mode (forge disabled)"
        return (
            "identity: forge mode using Zulip custom profile fields "
            f"({self.xmpp_field!r}, {self.telegram_field!r}, {self.signal_field!r})"
        )

    def _field_for(self, external_id: str, room: Optional[str]) -> Optional[str]:
        room_domain = ""
        if room and "@" in room:
            room_domain = room.split("@", 1)[1].lower()

        bare_domain = ""
        if "@" in external_id and not external_id.startswith("@"):
            bare_domain = external_id.split("@", 1)[1].lower()

        for domain in (room_domain, bare_domain):
            if TELEGRAM_DOMAIN_HINT in domain:
                return self.telegram_field
            if SIGNAL_DOMAIN_HINT in domain:
                return self.signal_field

        if external_id.startswith("@telegram_"):
            return self.telegram_field
        if external_id.startswith("@signal_"):
            return self.signal_field
        return self.xmpp_field

    def _directory_for(self, field_name: str) -> Dict[str, Mapping | None]:
        if self._directory is None:
            self._directory = self._load_directory()
        return self._directory.get(field_name, {})

    def _load_directory(self) -> Dict[str, Dict[str, Mapping | None]]:
        fields_result = self.bot_client.call_endpoint("/realm/profile_fields", method="GET")
        custom_fields = fields_result.get("custom_fields") or fields_result.get(
            "custom_profile_fields", []
        )

        field_ids = {
            field["name"]: str(field["id"])
            for field in custom_fields
            if field.get("name")
            in {self.xmpp_field, self.telegram_field, self.signal_field}
        }

        users_result = self.bot_client.get_users({"include_custom_profile_fields": True})
        users = users_result.get("members") or users_result.get("users") or []

        directory: Dict[str, Dict[str, Mapping | None]] = {
            self.xmpp_field: {},
            self.telegram_field: {},
            self.signal_field: {},
        }
        for user in users:
            if user.get("is_bot"):
                continue
            profile_data = user.get("profile_data") or {}
            mapping = Mapping(
                external_id="",
                zulip_email=user["email"],
                zulip_user_id=user.get("user_id") or user.get("id"),
                zulip_full_name=user.get("full_name"),
            )
            for field_name, field_id in field_ids.items():
                raw = profile_data.get(field_id) or profile_data.get(int(field_id))
                value = raw.get("value") if isinstance(raw, dict) else raw
                for key in self._normalized_values(value):
                    if key in directory[field_name]:
                        directory[field_name][key] = None
                    else:
                        directory[field_name][key] = Mapping(
                            external_id=key,
                            zulip_email=mapping.zulip_email,
                            zulip_user_id=mapping.zulip_user_id,
                            zulip_full_name=mapping.zulip_full_name,
                        )
        return directory

    def _candidates(self, external_id: str, display_name: Optional[str]) -> Iterable[str]:
        seen: set[str] = set()
        for source in (external_id, display_name):
            for value in self._normalized_values(source):
                if value not in seen:
                    seen.add(value)
                    yield value

    @staticmethod
    def _normalized_values(value: Any) -> list[str]:
        if value is None:
            return []
        text = str(value).strip()
        if not text:
            return []

        out = {text.lower()}
        if text.startswith("@") and len(text) > 1:
            out.add(text[1:].lower())
        if "@" in text:
            localpart, _, domain = text.partition("@")
            if localpart:
                out.add(localpart.lower())
            if domain:
                out.add(f"{localpart.lower()}@{domain.lower()}")
        return sorted(out)
