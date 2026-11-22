import pytest

from agents_common.envelope import CHAT_TAGS, parse


def one(text):
    p = parse(text, CHAT_TAGS)
    return p


def test_single_send():
    p = one('<chat-send room="r1">hello</chat-send>')
    assert p.errors == ()
    (e,) = p.envelopes
    assert (e.index, e.tag, e.attrs, e.body) == (1, "chat-send", {"room": "r1"}, "hello")


def test_multiple_in_one_block_share_numbering():
    text = (
        "intro\n<chat-send>a</chat-send>\n"
        '<chat-react to="m5" key="+1"/>\n'
        "<chat-status>x</chat-status>\n"
        "<chat-send>b</chat-send>\n"
    )
    p = one(text)
    assert [(e.index, e.tag) for e in p.envelopes] == [
        (1, "chat-send"),
        (2, "chat-react"),
        (4, "chat-send"),
    ]
    assert [e.index for e in p.errors] == [3]
    assert "chat-status" in p.errors[0].message


def test_leading_spaces_and_tabs():
    p = one("   <chat-send>a</chat-send>\n\t<chat-send>b</chat-send>")
    assert [e.body for e in p.envelopes] == ["a", "b"]


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_fenced_ignored(fence):
    p = one(f"{fence}\n<chat-send>a</chat-send>\n<chat-bogus>\n{fence}\n")
    assert p.envelopes == () and p.errors == ()


def test_fence_closes_and_later_tag_routes():
    p = one("```\n<chat-send>a</chat-send>\n```\n<chat-send>b</chat-send>")
    assert [e.body for e in p.envelopes] == ["b"]


def test_mismatched_fence_char_does_not_close():
    p = one("```\n~~~\n<chat-send>a</chat-send>\n")
    assert p.envelopes == ()


def test_unclosed_fence_runs_to_end():
    assert one("~~~\n<chat-send>a</chat-send>").envelopes == ()


def test_mid_line_tag_ignored():
    p = one("see <chat-send>a</chat-send> and `<chat-send>x</chat-send>`")
    assert p.envelopes == () and p.errors == ()


def test_raw_lt_amp_in_body_and_trim():
    p = one("<chat-send>\na < b && c <chat-send> d\n\n</chat-send>")
    assert p.errors == ()
    assert p.envelopes[0].body == "a < b && c <chat-send> d"


def test_nested_open_tag_literal_first_close_wins():
    p = one("<chat-send>x\n<chat-send>y</chat-send>\nz</chat-send>")
    assert [e.body for e in p.envelopes] == ["x\n<chat-send>y"]


def test_attribute_entities_single_pass():
    p = one('<chat-send room="a&amp;lt;&quot;&apos;&gt;">x</chat-send>')
    assert p.envelopes[0].attrs["room"] == "a&lt;\"'>"


def test_unknown_attribute():
    p = one('<chat-send foo="1">x</chat-send>')
    assert p.envelopes == ()
    assert "unknown attribute foo" in p.errors[0].message


def test_duplicate_attribute():
    p = one('<chat-send room="a" room="b">x</chat-send>')
    assert "duplicate attribute room" in p.errors[0].message


def test_reply_to_message_id_ok_and_send_id_error():
    assert one('<chat-send reply_to="m12">x</chat-send>').errors == ()
    p = one('<chat-send reply_to="s3">x</chat-send>')
    assert "send ids" in p.errors[0].message
    assert one('<chat-send reply_to="12">x</chat-send>').errors


def test_room_rules():
    assert one('<chat-send room="">x</chat-send>').errors
    assert one('<chat-send room="a b">x</chat-send>').errors
    assert one('<chat-send room="' + "a" * 256 + '">x</chat-send>').errors
    assert not one('<chat-send room="' + "a" * 255 + '">x</chat-send>').errors


def test_files_rules():
    assert one('<chat-send files="a.png,b/c.txt">x</chat-send>').errors == ()
    assert one('<chat-send files="a,b,c,d,e">x</chat-send>').errors
    assert one('<chat-send files="../a">x</chat-send>').errors
    assert one('<chat-send files="a/../b">x</chat-send>').errors
    assert one('<chat-send files="/etc/passwd">x</chat-send>').errors
    assert one('<chat-send files="a.png,,b">x</chat-send>').errors


def test_empty_body_needs_files():
    assert one("<chat-send></chat-send>").errors
    assert one("<chat-send>\n  \n</chat-send>").errors
    p = one('<chat-send files="a.png"></chat-send>')
    assert p.errors == () and p.envelopes[0].body == ""


def test_unclosed_send():
    p = one("<chat-send>hello")
    assert p.envelopes == ()
    assert "unclosed" in p.errors[0].message


def test_unknown_chat_tag_error_with_snippet_cap():
    p = one("<chat-status " + "x" * 200 + ">")
    assert len(p.errors[0].snippet) <= 80
    assert p.errors[0].index == 1


def test_non_family_tag_ignored():
    p = one("<div>x</div>\n<chatty>y</chatty>")
    assert p.envelopes == () and p.errors == ()


def test_react_ok_and_rules():
    p = one('<chat-react to="m3" key="eyes"/>')
    assert p.errors == ()
    assert p.envelopes[0].body is None
    assert p.envelopes[0].attrs == {"to": "m3", "key": "eyes"}
    assert "must be self-closing" in one('<chat-react to="m3" key="k">').errors[0].message
    assert one('<chat-react to="m3"/>').errors
    assert one('<chat-react key="k"/>').errors
    assert one('<chat-react to="s3" key="k"/>').errors
    assert one('<chat-react to="m3" key="' + "k" * 65 + '"/>').errors
    assert not one('<chat-react to="m3" key="' + "k" * 64 + '"/>').errors


def test_send_self_closing_is_error():
    assert one("<chat-send/>").errors


def test_body_cap():
    assert one("<chat-send>" + "a" * 20000 + "</chat-send>").errors == ()
    p = one("<chat-send>" + "a" * 20001 + "</chat-send>")
    assert "too long" in p.errors[0].message


def test_malformed_open_tag():
    p = one("<chat-send room=oops>x</chat-send>")
    assert p.envelopes == () and len(p.errors) == 1


def test_stray_closing_tag_ignored():
    p = one("</chat-send>\n<chat-send>a</chat-send>")
    assert [e.body for e in p.envelopes] == ["a"]


def test_registry_is_generic():
    from agents_common.envelope import Registry, TagSpec

    reg = Registry(
        tags={"x-a": TagSpec("x-a", False, {"to": lambda v: None}, frozenset({"to"}))},
        families=("x-",),
    )
    p = parse('<x-a to="q">b</x-a>\n<x-b>', reg)
    assert p.envelopes[0].tag == "x-a" and len(p.errors) == 1


def test_body_required_false_accepts_empty_body():
    from agents_common.envelope import Registry, TagSpec

    reg = Registry(
        tags={"x-a": TagSpec("x-a", False, {}, body_required=False)},
        families=("x-",),
    )
    p = parse("<x-a></x-a>", reg)
    assert p.errors == ()
    assert p.envelopes[0].body == ""


def test_shorter_closing_fence_does_not_close():
    p = one("````\n```\n<chat-send>a</chat-send>\n")
    assert p.envelopes == () and p.errors == ()


def test_longer_closing_fence_closes():
    p = one("```\n<chat-send>a</chat-send>\n`````\n<chat-send>b</chat-send>")
    assert [e.body for e in p.envelopes] == ["b"]


def test_backtick_fence_with_backtick_in_info_is_not_a_fence():
    p = one("``` a`b\n<chat-send>a</chat-send>")
    assert [e.body for e in p.envelopes] == ["a"]


def test_closing_fence_with_info_string_does_not_close():
    p = one("```\n``` js\n<chat-send>a</chat-send>\n")
    assert p.envelopes == ()


def test_entities_stay_literal_in_body():
    p = one("<chat-send>a &amp; b &lt; c</chat-send>")
    assert p.envelopes[0].body == "a &amp; b &lt; c"


def test_crlf_body_trimmed_like_lf():
    p = one("<chat-send>\r\nhello\r\nworld\r\n</chat-send>")
    assert p.errors == ()
    assert p.envelopes[0].body == "hello\r\nworld"


def test_snippet_is_opening_tag_only():
    p = one("<chat-send room=oops>secret body</chat-send>")
    assert p.errors[0].snippet == "<chat-send room=oops>"
    p = one('<chat-send bogus="1">secret body</chat-send>')
    assert p.errors[0].snippet == '<chat-send bogus="1">'
    p = one("<chat-send/>")
    assert p.errors[0].snippet == "<chat-send/>"


def test_snippet_unclosed_tag_is_line_and_capped():
    p = one("<chat-send room=" + "x" * 200 + "\nmore")
    assert p.errors[0].snippet == ("<chat-send room=" + "x" * 200)[:80]


def test_duplicate_attribute_reported():
    p = one('<chat-send room="a" room="b">x</chat-send>')
    assert "duplicate attribute room" in p.errors[0].message


# ---- hive tags ----

from agents_common.envelope import HIVE_TAGS, merge_registries  # noqa: E402


@pytest.mark.parametrize(
    "text",
    [
        '<session-send to="platform" handled="1,2" lane="idle">x</session-send>',
        '<agent-send to="leader" lane="priority" notify="idle">x</agent-send>',
        '<agent-send to="hive-2">x</agent-send>',
        '<project-post to="a,b" reply_to="p12" handled="41" project="proj">x</project-post>',
        "<project-post>x</project-post>",
    ],
)
def test_hive_tags_valid(text):
    p = parse(text, HIVE_TAGS)
    assert p.errors == () and len(p.envelopes) == 1


@pytest.mark.parametrize(
    "text",
    [
        "<session-send>x</session-send>",
        '<session-send to="Bad_Slug">x</session-send>',
        '<session-send to="' + "a" * 64 + '">x</session-send>',
        '<session-send to="a" handled="0">x</session-send>',
        '<session-send to="a" handled="1,x">x</session-send>',
        '<session-send to="a" handled="' + ",".join(["1"] * 51) + '">x</session-send>',
        '<session-send to="a" lane="bogus">x</session-send>',
        '<session-send to="a"></session-send>',
        '<session-send to="a">' + "x" * 16001 + "</session-send>",
        '<agent-send to="Leader">x</agent-send>',
        '<agent-send to="a" lane="bogus">x</agent-send>',
        '<agent-send to="a" notify="busy">x</agent-send>',
        '<project-post to="a,B">x</project-post>',
        '<project-post to="' + ",".join(["a"] * 21) + '">x</project-post>',
        '<project-post reply_to="p0">x</project-post>',
        '<project-post reply_to="q3">x</project-post>',
        "<project-post>" + "x" * 4001 + "</project-post>",
    ],
)
def test_hive_tags_invalid(text):
    p = parse(text, HIVE_TAGS)
    assert p.envelopes == () and len(p.errors) == 1


def test_priority_across_sessions_error():
    p = parse('<session-send to="a" lane="priority">x</session-send>', HIVE_TAGS)
    assert "priority is not allowed across sessions" in p.errors[0].message


def test_unknown_hive_tag_is_error():
    p = parse("<session-foo>x</session-foo>", HIVE_TAGS)
    assert "session-foo" in p.errors[0].message


def test_merged_registry_numbers_across_families():
    text = (
        "<chat-send>a</chat-send>\n"
        '<session-send to="b">b</session-send>\n'
        '<chat-react to="m1" key="+1"/>\n'
        "<project-post>c</project-post>\n"
    )
    p = parse(text, merge_registries(CHAT_TAGS, HIVE_TAGS))
    assert p.errors == ()
    assert [e.index for e in p.envelopes] == [1, 2, 3, 4]


def test_merge_registries_rejects_duplicate():
    with pytest.raises(ValueError):
        merge_registries(CHAT_TAGS, CHAT_TAGS)
