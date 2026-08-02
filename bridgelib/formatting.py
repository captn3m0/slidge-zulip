"""Formatting conversion between Matrix and Zulip.

Upstream ``matrix_bridge.py`` does no conversion whatsoever. It copies Matrix's
plain-text ``body`` straight into Zulip, and drops Zulip's message content straight into
Matrix's ``body``. Backticks then survive by luck in one direction and not at all in the
other, and nothing else (bold, lists, code blocks) survives either way.

Two directions, deliberately asymmetric:

Zulip -> Matrix
    We ask Zulip for *rendered HTML* (``apply_markdown=True`` on the event queue) rather
    than raw markdown. Zulip's markdown dialect has custom extensions -- user mentions,
    channel links, spoilers, LaTeX -- that a generic markdown renderer would mangle, so
    the server's own renderer is the only authoritative source. We then reduce that HTML
    to the subset Matrix clients accept.

Matrix -> Zulip
    Matrix's ``formatted_body`` is already HTML, and Zulip accepts markdown, so we
    convert HTML -> markdown. mautrix-telegram and mautrix-signal both emit proper
    ``<code>``/``<pre>`` for their native formatting, so this is where backticks survive
    the trip in from Telegram and Signal.

Note that requesting rendered HTML means upstream's media-link regex, which looks for
markdown ``[name](/user_uploads/...)`` syntax, can no longer match. ``find_upload_paths``
replaces it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import MarkdownConverter

# The HTML subset Matrix clients are expected to render, per the spec's "m.room.message"
# formatted body definition. Anything outside this is unwrapped to its text rather than
# dropped, so we never silently lose message content.
MATRIX_ALLOWED_TAGS = frozenset(
    {
        "a", "b", "blockquote", "br", "caption", "code", "del", "details", "div", "em",
        "font", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p",
        "pre", "span", "strike", "strong", "sub", "summary", "sup", "table", "tbody",
        "td", "th", "thead", "tr", "u", "ul",
    }
)

# Attributes kept per tag. Everything else is stripped -- notably Zulip's own `class`
# values, which mean nothing to a Matrix client and in some cases (codehilite) actively
# confuse them.
MATRIX_ALLOWED_ATTRS = {
    "a": {"href", "name", "target", "rel"},
    "img": {"src", "alt", "title", "width", "height"},
    "ol": {"start"},
    "code": {"class"},  # only language-* survives; see _clean_attrs
    "font": {"color", "data-mx-color", "data-mx-bg-color"},
    "span": {"data-mx-color", "data-mx-bg-color", "data-mx-spoiler"},
}

_SAFE_URL_SCHEMES = ("http://", "https://", "mxc://", "mailto:")

# Dropped with their contents rather than unwrapped. Everything else keeps its text, but
# unwrapping a <script> would turn its source into visible message text.
DROP_WITH_CONTENTS = frozenset({"script", "style", "head", "title", "meta", "link", "iframe"})


class _ZulipMarkdownConverter(MarkdownConverter):
    """markdownify tuned for Zulip's markdown dialect."""

    def convert_img(self, el: Tag, text: str, *args: Any, **kwargs: Any) -> str:
        # Matrix inlines emoji as images with the shortcode in `alt`. Zulip understands
        # `:shortcode:` natively, so emit that instead of an image link.
        alt = el.attrs.get("alt", "")
        if alt.startswith(":") and alt.endswith(":"):
            return alt
        return super().convert_img(el, text, *args, **kwargs)


def _code_language(el: Tag) -> str:
    """Pull `python` out of <code class="language-python">, for fenced code blocks."""
    for candidate in (el, el.find("code")):
        if not isinstance(candidate, Tag):
            continue
        for cls in candidate.attrs.get("class", []) or []:
            if cls.startswith("language-"):
                return cls[len("language-") :]
    return ""


def html_to_zulip_markdown(html: str) -> str:
    return _ZulipMarkdownConverter(
        heading_style="ATX",
        bullets="-",
        code_language_callback=_code_language,
    ).convert(html).strip()


def matrix_content_to_zulip_markdown(content: Dict[str, Any], fallback: str) -> str:
    """Best available Zulip markdown for a Matrix message's content dict.

    Prefers ``formatted_body`` (real HTML from the sending client or bridge); falls back
    to the plain ``body``, which is already markdown-ish by Matrix convention.
    """
    if content.get("format") == "org.matrix.custom.html" and content.get("formatted_body"):
        converted = html_to_zulip_markdown(content["formatted_body"])
        if converted:
            return converted
    return fallback


def _clean_attrs(tag: Tag, server_url: str) -> None:
    allowed = MATRIX_ALLOWED_ATTRS.get(tag.name, set())
    for name in list(tag.attrs):
        if name not in allowed:
            del tag.attrs[name]
            continue
        if name == "class":
            # Only language hints are meaningful downstream.
            kept = [c for c in tag.attrs["class"] or [] if c.startswith("language-")]
            if kept:
                tag.attrs["class"] = kept
            else:
                del tag.attrs["class"]

    # Resolve Zulip's root-relative URLs against the server *before* the scheme check,
    # otherwise every /user_uploads/ link looks unsafe and gets dropped.
    for name in ("href", "src"):
        value = tag.attrs.get(name)
        if value is None:
            continue
        if value.startswith("/"):
            value = urljoin(server_url, value)
            tag.attrs[name] = value
        if not value.startswith(_SAFE_URL_SCHEMES):
            del tag.attrs[name]


def sanitize_zulip_html(html: str, server_url: str) -> str:
    """Reduce Zulip's rendered HTML to something a Matrix client will display.

    Beyond dropping unknown tags this does four Zulip-specific fixups, each of which
    otherwise shows up as visible breakage in Telegram/Signal:

    * ``<div class="codehilite"><pre><span></span><code>`` is flattened to
      ``<pre><code>``. Zulip's syntax highlighting emits per-token ``<span>`` soup that
      no Matrix client understands.
    * Emoji images become their ``:shortcode:`` text. Matrix clients will not load
      images from a Zulip CDN they cannot authenticate to.
    * User/channel mention spans become plain text, keeping the ``@Name`` reading.
    * Relative links (``/user_uploads/...``, ``#narrow/...``) are made absolute against
      the Zulip server, since a Matrix client has no idea what they are relative to.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(list(DROP_WITH_CONTENTS)):
        tag.decompose()

    # Zulip wraps highlighted code in <div class="codehilite"><pre>...</pre></div> with
    # the actual text split across styling spans. Take the text, drop the soup.
    for div in soup.find_all("div", class_="codehilite"):
        language = ""
        data_lang = div.attrs.get("data-code-language")
        if data_lang:
            language = str(data_lang).lower()
        pre = soup.new_tag("pre")
        code = soup.new_tag("code")
        if language:
            code.attrs["class"] = [f"language-{language}"]
        code.string = div.get_text()
        pre.append(code)
        div.replace_with(pre)

    for img in soup.find_all("img"):
        classes = img.attrs.get("class") or []
        alt = img.attrs.get("alt", "")
        if "emoji" in classes and alt:
            img.replace_with(NavigableString(alt))

    for span in soup.find_all("span"):
        classes = span.attrs.get("class") or []
        if {"user-mention", "user-group-mention", "topic-mention"} & set(classes):
            span.replace_with(NavigableString(span.get_text()))

    for tag in soup.find_all(True):
        if tag.name not in MATRIX_ALLOWED_TAGS:
            tag.unwrap()
            continue
        _clean_attrs(tag, server_url)

    return str(soup).strip()


def zulip_html_to_matrix(html: str, server_url: str) -> Tuple[str, str]:
    """Return ``(plain_body, formatted_body)`` for a Zulip message.

    The plain body is markdown rather than bare text: clients that ignore
    ``formatted_body`` still see backticks and asterisks, and -- more importantly --
    mautrix relay mode falls back to the plain body when it cannot render.
    """
    formatted = sanitize_zulip_html(html, server_url)
    plain = html_to_zulip_markdown(formatted)
    return plain, formatted


def find_upload_paths(html: str) -> List[str]:
    """Zulip ``/user_uploads/...`` paths referenced by a rendered message.

    Replaces upstream's ``\\[[^\\[\\]]*\\]\\((/user_uploads/[^\\(\\)]*)\\)`` regex, which
    only matched raw markdown and therefore silently stopped finding anything once we
    switched the event queue to ``apply_markdown=True``.
    """
    soup = BeautifulSoup(html, "html.parser")
    paths: List[str] = []
    for anchor in soup.find_all("a"):
        href: Optional[str] = anchor.attrs.get("href")
        if href and href.startswith("/user_uploads/"):
            paths.append(href)
    for image in soup.find_all("img"):
        src: Optional[str] = image.attrs.get("src")
        if src and src.startswith("/user_uploads/"):
            paths.append(src)
    # Preserve order, drop duplicates -- Zulip renders an inline image preview as both an
    # <a> and an <img> pointing at the same upload.
    return list(dict.fromkeys(paths))


def strip_quotes_for_plain(text: str) -> str:
    """Collapse runs of blank lines, which markdownify is liberal with."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------------------
# XMPP
#
# XMPP formatting is XEP-0393 "message styling": plain text where `code`, *bold*,
# _italic_ and ~strike~ are rendered by the client. It is deliberately NOT XHTML-IM
# (XEP-0071) here -- XEP-0071's recommended tag profile has no <code> or <pre> at all, so
# sending it would make code *worse* in clients that prefer html over the plain body.
#
# The trap is that XEP-0393 and Zulip markdown disagree on the most common directive:
# XEP-0393 bold is *bold*, which Zulip reads as italic. Passing text through untouched
# silently changes emphasis in both directions, so both are translated explicitly.
# ---------------------------------------------------------------------------------


class _XmppStylingConverter(_ZulipMarkdownConverter):
    """markdownify that emits XEP-0393 styling rather than markdown."""

    def convert_strong(self, el: Tag, text: str, *args: Any, **kwargs: Any) -> str:
        return f"*{text}*" if text.strip() else text

    convert_b = convert_strong

    def convert_em(self, el: Tag, text: str, *args: Any, **kwargs: Any) -> str:
        return f"_{text}_" if text.strip() else text

    convert_i = convert_em

    def convert_del(self, el: Tag, text: str, *args: Any, **kwargs: Any) -> str:
        return f"~{text}~" if text.strip() else text

    convert_s = convert_del
    convert_strike = convert_del


def zulip_html_to_xmpp(html: str, server_url: str) -> str:
    """Zulip's rendered HTML -> XEP-0393 styled text for a groupchat body."""
    sanitized = sanitize_zulip_html(html, server_url)
    converted = _XmppStylingConverter(
        heading_style="ATX",
        bullets="-",
        code_language_callback=_code_language,
        escape_asterisks=False,
        escape_underscores=False,
    ).convert(sanitized)
    return strip_quotes_for_plain(converted)


# Fenced blocks first so their contents are never restyled, then inline code spans.
_PROTECTED = re.compile(r"(```.*?```|`[^`\n]+`)", re.DOTALL)


def xmpp_styling_to_zulip_markdown(text: str) -> str:
    """XEP-0393 styled text -> Zulip markdown.

    ``*bold*`` becomes ``**bold**`` and ``_italic_`` becomes ``*italic*``; without this,
    every bold word from XMPP would arrive in Zulip as italics. Code spans and fenced
    blocks are passed through untouched -- restyling their contents would corrupt the code
    itself, which is the one thing this project must not do.
    """

    def restyle(segment: str) -> str:
        # ~strike~ -> ~~strike~~
        segment = re.sub(r"(?<![\w~])~([^~\n]+)~(?![\w~])", r"~~\1~~", segment)
        # *bold* -> **bold**
        segment = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"**\1**", segment)
        # _italic_ -> *italic*. The word-boundary guards keep snake_case_names intact.
        segment = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"*\1*", segment)
        return segment

    return "".join(
        part if index % 2 else restyle(part)
        for index, part in enumerate(_PROTECTED.split(text))
    )


def xmpp_message_to_zulip_markdown(body: str, xhtml: Optional[str] = None) -> str:
    """Best available Zulip markdown for an inbound XMPP message.

    Prefers XHTML-IM when a client sends it, since real markup beats guessing at styling
    directives; otherwise translates the XEP-0393 plain body.
    """
    if xhtml:
        converted = html_to_zulip_markdown(xhtml)
        if converted:
            return converted
    return xmpp_styling_to_zulip_markdown(body).strip()
