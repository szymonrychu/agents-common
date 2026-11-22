import pytest

from agents_common.frames import message_in, notice


def test_message_in_shape_and_attr_order():
    out = message_in("session", "a", "12", "hi", "reply via x", reply_to="p3", post=None, for_="a9")
    assert out == (
        '<message-in channel="session" from="a" id="12" reply_to="p3" for="a9">\n'
        "hi\n</message-in>\nreply via x\n"
    )


def test_message_in_escapes_attr_quote_and_body_lt():
    out = message_in("room", 'x"y', "1", "a < b & c", "t")
    assert 'from="x&quot;y"' in out
    assert "\na &lt; b &amp; c\n" in out


def test_notice_shape():
    assert notice("s-2", "a99", "failed", "hop_limit") == (
        '<message-in channel="notice" from="s-2" for="a99" status="failed" reason="hop_limit"/>\n'
    )
    assert notice("s-2", "a99", "idle") == (
        '<message-in channel="notice" from="s-2" for="a99" status="idle"/>\n'
    )


def test_notice_rejects_unknown_status():
    with pytest.raises(ValueError):
        notice("s", "a1", "bogus")
