"""Sender identity resolution through Zulip custom profile fields."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import zulip

from bridgelib.identity import (
    DEFAULT_SIGNAL_FIELD,
    DEFAULT_TELEGRAM_FIELD,
    DEFAULT_XMPP_FIELD,
    IdentityConfigError,
    IdentityResolver,
    MIRROR_CLIENT_NAME,
)

SITE = "https://521ab.zulipchat.com"
BOT_EMAIL = "bridge-relay-bot@521ab.zulipchat.com"


@dataclass
class DummyClient:
    email: str
    api_key: str
    site: str
    client_name: str = ""


class FakeBot:
    def __init__(self, fields=None, users=None):
        self.fields = fields or [
            {"id": 1, "name": DEFAULT_XMPP_FIELD},
            {"id": 2, "name": DEFAULT_TELEGRAM_FIELD},
            {"id": 3, "name": DEFAULT_SIGNAL_FIELD},
        ]
        self.users = users or []

    def call_endpoint(self, url, method="POST", request=None, **kwargs):
        assert url == "/realm/profile_fields"
        assert method == "GET"
        return {"custom_profile_fields": self.fields}

    def get_users(self, request=None):
        assert request == {"include_custom_profile_fields": True}
        return {"members": self.users}


@pytest.fixture
def patch_zulip_client(monkeypatch):
    monkeypatch.setattr(
        zulip,
        "Client",
        lambda email, api_key, site, client=None: DummyClient(
            email=email,
            api_key=api_key,
            site=site,
            client_name=client or "",
        ),
    )


def resolver(bot, **kwargs):
    return IdentityResolver(
        forge=kwargs.pop("forge", False),
        site=SITE,
        bot_client=bot,
        forge_email=BOT_EMAIL,
        forge_api_key="botkey",
        **kwargs,
    )


class TestConfiguration:
    def test_forge_requires_credentials(self):
        with pytest.raises(IdentityConfigError, match="requires Zulip email and API key"):
            IdentityResolver(
                forge=True,
                site=SITE,
                bot_client=FakeBot(),
                forge_email="",
                forge_api_key="",
            )

    def test_forge_client_announces_a_mirror_client_name(self, patch_zulip_client):
        res = resolver(FakeBot(), forge=True)
        assert res._forge_client.client_name == MIRROR_CLIENT_NAME


class TestResolution:
    def test_prefix_mode_never_impersonates(self, patch_zulip_client):
        user = {
            "email": "alice@example.com",
            "user_id": 42,
            "full_name": "Alice Kapoor",
            "profile_data": {"1": {"value": "alice@localhost"}},
        }
        assert resolver(FakeBot(users=[user]), forge=False).resolve("alice@localhost") is None

    def test_unmapped_sender_falls_back(self, patch_zulip_client):
        res = resolver(FakeBot(users=[]), forge=True)
        assert res.resolve("alice@localhost", room="general@conference.localhost") is None

    def test_xmpp_field_can_match_bare_jid(self, patch_zulip_client):
        user = {
            "email": "alice@example.com",
            "user_id": 42,
            "full_name": "Alice Kapoor",
            "profile_data": {"1": {"value": "alice@localhost"}},
        }
        resolved = resolver(FakeBot(users=[user]), forge=True).resolve(
            "alice@localhost", room="general@conference.localhost"
        )
        assert resolved.extra == {"forged": "true", "sender": "alice@example.com"}

    def test_xmpp_field_can_match_localpart(self, patch_zulip_client):
        user = {
            "email": "alice@example.com",
            "user_id": 42,
            "full_name": "Alice Kapoor",
            "profile_data": {"1": {"value": "alice"}},
        }
        resolved = resolver(FakeBot(users=[user]), forge=True).resolve(
            "alice@localhost", room="general@conference.localhost"
        )
        assert resolved.extra == {"forged": "true", "sender": "alice@example.com"}

    def test_telegram_field_uses_gateway_domain(self, patch_zulip_client):
        user = {
            "email": "alice@example.com",
            "user_id": 42,
            "full_name": "Alice Kapoor",
            "profile_data": {"2": {"value": "@alice_telegram"}},
        }
        resolved = resolver(FakeBot(users=[user]), forge=True).resolve(
            "123456789@telegram.localhost",
            room="some-room@telegram.localhost",
            display_name="alice_telegram",
        )
        assert resolved.extra == {"forged": "true", "sender": "alice@example.com"}

    def test_signal_field_uses_gateway_domain(self, patch_zulip_client):
        user = {
            "email": "alice@example.com",
            "user_id": 42,
            "full_name": "Alice Kapoor",
            "profile_data": {"3": {"value": "+15551234567"}},
        }
        resolved = resolver(FakeBot(users=[user]), forge=True).resolve(
            "+15551234567@signal.localhost",
            room="some-room@signal.localhost",
        )
        assert resolved.extra == {"forged": "true", "sender": "alice@example.com"}

    def test_ambiguous_field_value_refuses_to_impersonate(self, patch_zulip_client):
        users = [
            {
                "email": "alice@example.com",
                "user_id": 42,
                "full_name": "Alice Kapoor",
                "profile_data": {"1": {"value": "alice@localhost"}},
            },
            {
                "email": "other@example.com",
                "user_id": 99,
                "full_name": "Other User",
                "profile_data": {"1": {"value": "alice@localhost"}},
            },
        ]
        assert (
            resolver(FakeBot(users=users), forge=True).resolve(
                "alice@localhost", room="general@conference.localhost"
            )
            is None
        )
