# slidge-zulip

Bridges one Zulip stream/topic to a set of XMPP MUCs, with XMPP as the hub.

Typical use:

- native XMPP users in one room
- a Telegram room exposed by **Slidgram**
- a Signal room exposed by **Slidgnal**

The bridge joins all configured rooms and relays them into the same Zulip conversation.

## Identity

`forge = false` is the default and needs no special Zulip support: messages relay through
the bot account with a visible sender prefix.

`forge = true` uses Zulip's mirroring API to post as real Zulip users. Sender resolution
comes from Zulip custom profile fields:

- `XMPP Username`
- `Telegram Username`
- `Signal Username`

If a sender does not match exactly one user, the bridge falls back to the visible prefix.

## Local testing

```bash
uv run pytest
dev/with-prosody.sh uv run pytest -m integration
```

## Image

```bash
docker build -t ghcr.io/captn3m0/slidge-zulip:latest .
```
