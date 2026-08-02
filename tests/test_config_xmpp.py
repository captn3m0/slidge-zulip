"""Config parsing for the XMPP-first bridge."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge-xmpp"))
from zulip_xmpp_bridge import BridgeConfigError, read_configuration  # noqa: E402

ROOM = "general@conference.localhost"

CONFIG = """
    [xmpp]
    jid = bridge@localhost
    password = bridgepass
    rooms =
        {room}
        other@conference.localhost
    host = 127.0.0.1
    port = 5222
    ca_certs = dev/prosody/certs/localhost.crt

    [zulip]
    email = bridge-relay-bot@521ab.zulipchat.com
    api_key = aPiKeY
    site = https://521ab.zulipchat.com
    stream = general
    topic = general-chat

    [identity]
    forge = true
"""


def write_config(tmp_path, body):
    path = tmp_path / "bridge.conf"
    path.write_text(textwrap.dedent(body))
    return str(path)


class TestConfig:
    def test_rooms_are_split_from_a_multiline_value(self, tmp_path):
        config = read_configuration(write_config(tmp_path, CONFIG.format(room=ROOM)))
        assert config["xmpp"]["rooms"] == [ROOM, "other@conference.localhost"]

    def test_duplicate_rooms_are_rejected(self, tmp_path):
        body = CONFIG.format(room=ROOM).replace("other@conference.localhost", ROOM)
        with pytest.raises(BridgeConfigError, match="duplicates"):
            read_configuration(write_config(tmp_path, body))

    def test_identity_forge_flag_is_read(self, tmp_path):
        config = read_configuration(write_config(tmp_path, CONFIG.format(room=ROOM)))
        assert config["identity"]["forge"] == "true"


class TestShippedExample:
    @pytest.fixture
    def example(self):
        return read_configuration(
            str(Path(__file__).resolve().parent.parent / "bridge-xmpp/bridge.conf.example")
        )

    def test_it_parses(self, example):
        assert example["xmpp"]["jid"] == "bridge@localhost"
        assert example["zulip"]["stream"] == "general"

    def test_it_points_at_multiple_rooms(self, example):
        assert len(example["xmpp"]["rooms"]) == 3

    def test_it_includes_signal_and_telegram_gateway_domains(self, example):
        assert "some-signal-chat@signal.localhost" in example["xmpp"]["rooms"]
        assert "some-telegram-chat@telegram.localhost" in example["xmpp"]["rooms"]

    def test_it_defaults_to_prefix_mode(self, example):
        assert example["identity"]["forge"] == "false"
