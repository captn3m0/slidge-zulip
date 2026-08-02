"""Transport-agnostic pieces shared by the Zulip bridges.

Everything in here deals with Zulip and with bookkeeping, never with the chat network on
the other side. That separation is deliberate: the network-facing half has been rewritten
once already (Matrix -> XMPP) and these modules survived the change untouched apart from
prose.

- ``identity``   -- map a foreign sender id to a Zulip account, and post as them.
- ``provenance`` -- remember which Zulip messages we sent, so they do not echo.
"""
