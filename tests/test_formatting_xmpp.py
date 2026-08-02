"""XMPP formatting round-trips.

The requirement: a backtick typed in Signal or Telegram must reach Zulip as a code span,
and a code span written in Zulip must reach those networks as monospace.

The subtle half is emphasis. XEP-0393 says bold is ``*bold*``; Zulip markdown reads that
as *italic*. Relaying either direction untranslated silently changes what people wrote.
"""

from bridgelib.formatting import (
    xmpp_message_to_zulip_markdown,
    xmpp_styling_to_zulip_markdown,
    zulip_html_to_xmpp,
)

SERVER = "https://521ab.zulipchat.com"


class TestXmppToZulip:
    def test_code_span_passes_through(self):
        assert xmpp_styling_to_zulip_markdown("try `ls -la`") == "try `ls -la`"

    def test_bold_is_promoted_not_downgraded_to_italic(self):
        # The whole reason this translation exists.
        assert xmpp_styling_to_zulip_markdown("*bold*") == "**bold**"

    def test_italic_becomes_single_asterisk(self):
        assert xmpp_styling_to_zulip_markdown("_soft_") == "*soft*"

    def test_strike_becomes_double_tilde(self):
        assert xmpp_styling_to_zulip_markdown("~gone~") == "~~gone~~"

    def test_snake_case_is_not_mangled_into_italics(self):
        assert xmpp_styling_to_zulip_markdown("call some_function_name now") == (
            "call some_function_name now"
        )

    def test_styling_inside_a_code_span_is_left_alone(self):
        # Restyling code contents would corrupt the code, which is the one unforgivable bug.
        assert xmpp_styling_to_zulip_markdown("`a * b * c`") == "`a * b * c`"

    def test_styling_inside_a_fenced_block_is_left_alone(self):
        src = "```\nx = *p;\ny = _q;\n```"
        assert xmpp_styling_to_zulip_markdown(src) == src

    def test_mixed_text_around_a_code_span(self):
        assert xmpp_styling_to_zulip_markdown("run `make *` then *stop*") == (
            "run `make *` then **stop**"
        )

    def test_xhtml_is_preferred_when_present(self):
        assert xmpp_message_to_zulip_markdown(
            "plain fallback", "<p>try <code>ls</code></p>"
        ) == "try `ls`"

    def test_falls_back_to_the_plain_body(self):
        assert xmpp_message_to_zulip_markdown("*bold*", None) == "**bold**"


class TestZulipToXmpp:
    def test_code_span_becomes_backticks(self):
        assert "`ls -la`" in zulip_html_to_xmpp("<p>try <code>ls -la</code></p>", SERVER)

    def test_bold_becomes_single_asterisk(self):
        # XEP-0393 bold, not markdown bold.
        assert zulip_html_to_xmpp("<p><strong>loud</strong></p>", SERVER) == "*loud*"

    def test_italic_becomes_underscores(self):
        assert zulip_html_to_xmpp("<p><em>soft</em></p>", SERVER) == "_soft_"

    def test_strike_becomes_single_tilde(self):
        assert zulip_html_to_xmpp("<p><del>gone</del></p>", SERVER) == "~gone~"

    def test_code_block_is_fenced(self):
        zulip = (
            '<div class="codehilite" data-code-language="Python">'
            '<pre><span></span><code>print("hi")\n</code></pre></div>'
        )
        result = zulip_html_to_xmpp(zulip, SERVER)
        assert "```" in result
        assert 'print("hi")' in result

    def test_mention_reads_as_plain_text(self):
        zulip = '<p><span class="user-mention" data-user-id="9">@Alice</span> hi</p>'
        assert "@Alice" in zulip_html_to_xmpp(zulip, SERVER)

    def test_upload_link_is_absolute(self):
        # A relative link is useless to a Signal or Telegram client.
        zulip = '<p><a href="/user_uploads/2/ab/notes.txt">notes.txt</a></p>'
        assert f"{SERVER}/user_uploads/2/ab/notes.txt" in zulip_html_to_xmpp(zulip, SERVER)

    def test_emoji_becomes_shortcode(self):
        zulip = '<p>nice <img alt=":tada:" class="emoji" src="/static/emoji/x.png"></p>'
        assert ":tada:" in zulip_html_to_xmpp(zulip, SERVER)


class TestRoundTrip:
    """What a user actually experiences, end to end."""

    def test_code_survives_zulip_to_xmpp_to_zulip(self):
        once = zulip_html_to_xmpp("<p>run <code>docker compose up -d</code></p>", SERVER)
        assert xmpp_message_to_zulip_markdown(once) == "run `docker compose up -d`"

    def test_bold_survives_the_round_trip(self):
        once = zulip_html_to_xmpp("<p><strong>ship it</strong></p>", SERVER)
        assert once == "*ship it*"
        assert xmpp_message_to_zulip_markdown(once) == "**ship it**"
