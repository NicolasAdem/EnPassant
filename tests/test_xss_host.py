"""Task A2 regression: XSS via player name in host dashboard.

The original bug was at templates/host.html:245, where player names were
interpolated into an inline onclick after EP.escape():

    onclick="removePlayer('${p.id}', '${EP.escape(p.name).replace(...)}')"

A name like  );alert(1);//  becomes  &#39;);alert(1);//  in HTML, which
the browser HTML-decodes back to  '  inside the onclick attribute
*before* the JS parser sees it. The fix removed all inline handlers from
host.html and routed events through data-* attributes + delegated
listeners on the stable parent containers. As long as no inline event
handler attributes exist in the rendered HTML, the HTML-escape-then-
decode trap is structurally impossible.

This test enforces that invariant statically on the template file."""

import os
import re

HERE = os.path.dirname(__file__)
HOST_HTML = os.path.normpath(os.path.join(HERE, "..", "templates", "host.html"))


def _read_template():
    with open(HOST_HTML, "r", encoding="utf-8") as f:
        return f.read()


# Inline event handler attributes: any  onXxx=  in the template, whether
# in literal HTML or inside a JS template string. The browser fires them
# either way once the HTML lands in the DOM via innerHTML, so the
# distinction doesn't matter for XSS purposes.
INLINE_HANDLER_RE = re.compile(
    r"""\bon[a-z]+\s*=\s*["']""",
    re.IGNORECASE,
)


def test_no_inline_event_handlers_in_host_template():
    """No onclick=, onchange=, onkeydown=, onerror=, onload=, ... anywhere.

    This is the structural guarantee that A2 cannot regress. The fix
    moved every handler off the rendered HTML and onto delegated
    listeners attached to the standings / matches containers."""
    src = _read_template()
    matches = INLINE_HANDLER_RE.findall(src)
    assert not matches, (
        f"Found {len(matches)} inline event handler attribute(s) in "
        f"host.html: {matches}. Inline handlers re-introduce the "
        f"HTML-escape-then-decode XSS vector fixed in A2. Use a "
        f"data-action attribute and a delegated listener instead."
    )


def test_player_name_never_interpolated_into_js_string_literal():
    """No  '${...p.name...}'  or  '${...m.white_name...}'  patterns.

    Even with HTML-escaping, putting a user-controlled value inside a
    JS string literal that is itself inside an HTML attribute fails
    because the HTML parser decodes &#39; back to ' before the JS
    parser runs. The fix put names in element text and data-* attribute
    values only — never inside a JS string."""
    src = _read_template()

    # Match either a single-quoted or double-quoted JS string that
    # contains a ${...} interpolation referencing a name-like field.
    # The (?<!=) negative lookbehind excludes HTML attribute values
    # (e.g. data-name="${EP.escape(p.name)}"), which are the SAFE
    # location the fix moved names into. The vulnerable pattern is
    # the JS string literal form ('${...}' or "${...}") where the
    # quote is the JS string delimiter, not the HTML attribute
    # delimiter.
    risky_fields = [
        r"\.name\b",
        r"\.white_name\b",
        r"\.black_name\b",
    ]
    for field in risky_fields:
        pattern = re.compile(
            r"""(?<!=)['"]\$\{[^}]*""" + field + r"""[^}]*\}['"]""",
        )
        m = pattern.search(src)
        assert m is None, (
            f"host.html interpolates a player-name field ({field}) "
            f"inside a JS string literal at offset {m.start()}: "
            f"{m.group(0)!r}. This is the A2 vector — even with "
            f"EP.escape() the HTML parser decodes the entities back "
            f"before the JS parser runs. Use a data-* attribute and "
            f"read it via dataset in a delegated listener."
        )


def test_player_id_never_interpolated_into_js_string_literal():
    """Same guarantee for ID fields.

    Player IDs and match IDs aren't currently user-controlled in the
    schema, but the same anti-pattern was present at line 245 for
    p.id and at lines 314-316 for m.id. Removing it here keeps the
    template honest if the ID generation ever changes."""
    src = _read_template()
    risky_fields = [r"\.id\b"]
    for field in risky_fields:
        # Allow `${m.id}` etc. inside attribute *values* like
        # data-mid="${...}" — those are safe (HTML attribute context).
        # The bad pattern is `'${...}'` or `"${...}"` where the
        # quote is the JS string delimiter, not the HTML attribute
        # delimiter. We detect this by looking for the pattern with
        # NO `=` immediately before the opening quote.
        pattern = re.compile(
            r"""(?<!=)['"]\$\{[^}]*""" + field + r"""[^}]*\}['"]""",
        )
        m = pattern.search(src)
        assert m is None, (
            f"host.html interpolates an ID field ({field}) inside a "
            f"JS string literal at offset {m.start()}: {m.group(0)!r}. "
            f"Even though IDs are server-generated today, the pattern "
            f"is the A2 anti-pattern — keep IDs in data-* attributes."
        )


def test_render_standings_function_uses_data_attributes_for_remove_button():
    """The remove-player button must use data-action and data-pid/data-name.

    This is the positive form of the invariant: the fix is in place
    AND uses the documented attribute names that the delegated
    listener at the bottom of the template reads from."""
    src = _read_template()
    # The remove button is generated in the standings table cell.
    # We expect both data-action="remove-player" and data-pid present.
    assert 'data-action="remove-player"' in src, (
        "Remove-player button is missing data-action — the delegated "
        "listener won't fire."
    )
    assert "data-pid=" in src, "Remove-player button is missing data-pid."
    assert "data-name=" in src, "Remove-player button is missing data-name."


def test_render_matches_uses_data_attributes_for_host_resolve():
    """Host-resolve override buttons must use data-action/data-mid/data-result."""
    src = _read_template()
    assert 'data-action="host-resolve"' in src
    assert "data-mid=" in src
    assert 'data-result="white"' in src
    assert 'data-result="draw"' in src
    assert 'data-result="black"' in src


# ------------------------------------------------------------------
# The "render with a hostile name" check, done by simulating what the
# template would produce. We don't need a JS engine — we just need to
# verify that with a representative hostile input, the resulting HTML
# string contains nothing that would execute as JS. The template uses
# EP.escape() for HTML-attribute contexts, which is the canonical fix.
# ------------------------------------------------------------------

def _ep_escape(s):
    """Mirror of EP.escape from static/js/common.js — same character set."""
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


HOSTILE_NAMES = [
    # The exact payload called out in tasks.txt A2.
    "');alert(1);//",
    # Classic <script>-tag injection.
    "<script>alert(1)</script>",
    # Image with onerror — would fire on any img-rendering context.
    '<img src=x onerror=alert(1)>',
    # Double-quote breakout attempt.
    '" onclick="alert(1)" x="',
    # Mixed-case obfuscation.
    "<ScRiPt>alert(1)</ScRiPt>",
]


def _simulate_remove_button(player_id, player_name):
    """Reproduce the post-fix template output for the standings remove cell.

    This is a faithful copy of the JS template literal at host.html:245,
    with EP.escape implemented in Python. If the template ever changes,
    these strings should be updated to match.
    """
    return (
        f'<button class="btn btn-ghost" '
        f'style="padding: 0.25rem 0.5rem; font-size: 0.75rem;" '
        f'data-action="remove-player" '
        f'data-pid="{_ep_escape(player_id)}" '
        f'data-name="{_ep_escape(player_name)}">×</button>'
    )


def test_hostile_names_render_safely_in_remove_button():
    """Hostile player names produce no executable script vectors."""
    for name in HOSTILE_NAMES:
        html = _simulate_remove_button("p123", name)
        # Critical: no inline event handler attributes in the output.
        assert not INLINE_HANDLER_RE.search(html), (
            f"Hostile name {name!r} produced an inline event handler "
            f"in the rendered HTML: {html!r}"
        )
        # Critical: no raw <script tags injected. (Case-insensitive
        # because <ScRiPt> etc. is a known obfuscation.)
        assert "<script" not in html.lower(), (
            f"Hostile name {name!r} produced a literal <script tag: "
            f"{html!r}"
        )
        # Critical: the hostile payload appears in the output ONLY in
        # its entity-encoded form. We check this by recovering the
        # data-name attribute value and asserting that what comes out
        # of HTML-decoding it equals the original — i.e. the payload
        # round-trips as data, not as parsed HTML/JS.
        m = re.search(r'data-name="([^"]*)"', html)
        assert m is not None, f"data-name attribute missing in: {html!r}"
        attr_value = m.group(1)
        decoded = (attr_value
                   .replace("&#39;", "'")
                   .replace("&quot;", '"')
                   .replace("&gt;", ">")
                   .replace("&lt;", "<")
                   .replace("&amp;", "&"))
        assert decoded == name, (
            f"Hostile name {name!r} did not round-trip through the "
            f"data-name attribute. Got attr={attr_value!r}, "
            f"decoded={decoded!r}."
        )
        # And the raw payload, if it contained HTML-special chars,
        # must NOT appear verbatim outside the data-name attribute.
        outside_attr = html.replace(f'data-name="{attr_value}"', "")
        for ch in ("<", ">", '"', "'"):
            if ch in name:
                # The character may legitimately appear in HTML
                # structure (e.g. `>` ending a tag) but the hostile
                # *substring* must not survive verbatim.
                pass
        if any(c in name for c in "<>"):
            assert name not in outside_attr, (
                f"Hostile name {name!r} appears verbatim outside the "
                f"data-name attribute: {outside_attr!r}"
            )