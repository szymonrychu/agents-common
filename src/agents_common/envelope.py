"""Registry-driven parser for tag envelopes in agent text.

Pure and stdlib-only; it knows no room format and no transport. A `Registry` maps tag names to a
`TagSpec` and lists family prefixes: a line-start tag of a registered family whose name is not
registered is an error, so a guessed tag is reported instead of shown silently.

Grammar (shared by every registry):

- A tag is recognised only at a line start (leading spaces or tabs allowed), outside fenced code
  blocks (``` and ~~~, CommonMark open/close pairing).
- Attributes are double-quoted, each name at most once, unknown names are errors; values unescape
  only `&amp; &lt; &gt; &quot; &apos;`.
- A body is raw text up to the first closing tag (no entity decoding, a nested opening tag is
  literal). One newline directly after the opening tag and trailing whitespace are trimmed.
- `index` is the 1-based ordinal of the opening tag among all recognised opening tags, valid or
  not, so envelopes and errors share one numbering.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

Validator = Callable[[str], str | None]

SNIPPET_MAX = 80


@dataclass(frozen=True)
class TagSpec:
    name: str
    self_closing: bool
    attrs: Mapping[str, Validator]
    required: frozenset[str] = frozenset()
    body_max: int | None = None
    # A non-self-closing body must be non-empty unless the attribute named here is present.
    body_required_unless: str | None = None
    # Set False for tags whose body may always be empty.
    body_required: bool = True


@dataclass(frozen=True)
class Registry:
    tags: Mapping[str, TagSpec]
    families: tuple[str, ...]


@dataclass(frozen=True)
class Envelope:
    index: int
    tag: str
    attrs: Mapping[str, str]
    body: str | None


@dataclass(frozen=True)
class EnvelopeError:
    index: int
    snippet: str
    message: str


@dataclass(frozen=True)
class Parsed:
    envelopes: tuple[Envelope, ...]
    errors: tuple[EnvelopeError, ...]


_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'"}
_ENTITY_RE = re.compile("&(?:amp|lt|gt|quot|apos);")
_OPEN_RE = re.compile(r"<([A-Za-z][A-Za-z0-9_-]*)(?=[\s/>]|$)")
_ATTR_RE = re.compile(r'\s+([A-Za-z_][A-Za-z0-9_-]*)="([^"]*)"')
_END_RE = re.compile(r"\s*(/?)>")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def unescape(value: str) -> str:
    """Unescape the five supported entities in one pass."""
    return _ENTITY_RE.sub(lambda m: _ENTITIES[m.group(0)], value)


def _snippet(text: str, pos: int) -> str:
    """The opening tag only: up to its first `>` (or the line end if unclosed), capped."""
    line = text[pos:].split("\n", 1)[0].rstrip("\r")
    gt = line.find(">")
    if gt != -1:
        line = line[: gt + 1]
    return line[:SNIPPET_MAX]


def parse(text: str, registry: Registry) -> Parsed:
    envelopes: list[Envelope] = []
    errors: list[EnvelopeError] = []
    index = 0
    fence: tuple[str, int] | None = None  # (char, length) of the open fence
    pos = 0
    n = len(text)
    while pos < n:
        nl = text.find("\n", pos)
        line_end = n if nl == -1 else nl + 1
        line = text[pos:line_end].rstrip("\r\n")
        if fence is not None:
            m = _FENCE_RE.match(line)
            if (
                m
                and m.group(1)[0] == fence[0]
                and len(m.group(1)) >= fence[1]
                and not m.group(2).strip()
            ):
                fence = None
            pos = line_end
            continue
        m = _FENCE_RE.match(line)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)))
            pos = line_end
            continue
        stripped = line.lstrip(" \t")
        start = pos + (len(line) - len(stripped))
        om = _OPEN_RE.match(text, start)
        if om is None or start >= line_end:
            pos = line_end
            continue
        name = om.group(1)
        spec = registry.tags.get(name)
        if spec is None:
            if any(name.startswith(f) for f in registry.families):
                index += 1
                errors.append(EnvelopeError(index, _snippet(text, start), f"unknown tag <{name}>"))
            pos = line_end
            continue
        index += 1
        snippet = _snippet(text, start)
        attrs_raw, dups, tag_end, self_close, err = _read_open_tag(text, om.end())
        if err is not None or attrs_raw is None:
            errors.append(EnvelopeError(index, snippet, err or "malformed opening tag"))
            pos = line_end
            continue
        problems = _check_attrs(spec, attrs_raw, dups)
        body: str | None = None
        resume = tag_end
        if spec.self_closing:
            if not self_close:
                problems.append(f"<{name}> must be self-closing (end with />)")
        else:
            if self_close:
                problems.append(f"<{name}> is not self-closing; it needs </{name}>")
                body = None
            else:
                close = text.find(f"</{name}>", tag_end)
                if close == -1:
                    errors.append(
                        EnvelopeError(index, snippet, f"unclosed <{name}>: missing </{name}>")
                    )
                    pos = line_end
                    continue
                body = _trim_body(text[tag_end:close])
                resume = close + len(name) + 3
                problems.extend(_check_body(spec, attrs_raw, body))
        if problems:
            errors.append(EnvelopeError(index, snippet, "; ".join(problems)))
        else:
            envelopes.append(Envelope(index, name, dict(attrs_raw), body))
        # Continue on the line after the end of what was consumed.
        nl = text.find("\n", max(resume, pos))
        pos = n if nl == -1 else nl + 1
    return Parsed(tuple(envelopes), tuple(errors))


def _read_open_tag(
    text: str, pos: int
) -> tuple[dict[str, str] | None, list[str], int, bool, str | None]:
    """Read attributes and the tag end from `pos`.

    Returns (attrs, duplicate names, end, self_close, error).
    """
    attrs: dict[str, str] = {}
    dups: list[str] = []
    while True:
        am = _ATTR_RE.match(text, pos)
        if am is None:
            break
        key = am.group(1)
        if key in attrs:
            dups.append(key)
        attrs[key] = unescape(am.group(2))
        pos = am.end()
    em = _END_RE.match(text, pos)
    if em is None:
        return None, dups, pos, False, 'malformed opening tag (attributes must be name="value")'
    return attrs, dups, em.end(), em.group(1) == "/", None


def _check_attrs(spec: TagSpec, attrs: dict[str, str], dups: list[str]) -> list[str]:
    problems: list[str] = [f"duplicate attribute {d}" for d in dups]
    for key, value in attrs.items():
        validator = spec.attrs.get(key)
        if validator is None:
            problems.append(f"unknown attribute {key}")
            continue
        msg = validator(value)
        if msg:
            problems.append(f"attribute {key}: {msg}")
    for key in sorted(spec.required):
        if key not in attrs:
            problems.append(f"missing required attribute {key}")
    return problems


def _trim_body(body: str) -> str:
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return body.rstrip()


def _check_body(spec: TagSpec, attrs: Mapping[str, str], body: str) -> list[str]:
    problems: list[str] = []
    waived = spec.body_required_unless is not None and spec.body_required_unless in attrs
    if spec.body_required and not body and not waived:
        hint = f" unless {spec.body_required_unless} is given" if spec.body_required_unless else ""
        problems.append(f"empty body{hint}")
    if spec.body_max is not None and len(body) > spec.body_max:
        problems.append(f"body too long ({len(body)} > {spec.body_max} chars)")
    return problems


# ---- chat tags ------------------------------------------------------------------------------

_MSG_ID_RE = re.compile(r"m[0-9]+")
_SEND_ID_RE = re.compile(r"s[0-9]+")


def message_id(value: str) -> str | None:
    """`m<digits>` only; a send id (`s<n>`) gets a specific error."""
    if _MSG_ID_RE.fullmatch(value):
        return None
    if _SEND_ID_RE.fullmatch(value):
        return "send ids (s<n>) are not supported; use a message id m<n>"
    return "must be a message id m<digits>"


def room(value: str) -> str | None:
    if not value:
        return "must not be empty"
    if any(c.isspace() for c in value):
        return "must not contain whitespace"
    if len(value) > 255:
        return "at most 255 chars"
    return None


FILES_MAX = 4


def files(value: str) -> str | None:
    paths = value.split(",")
    if len(paths) > FILES_MAX:
        return f"at most {FILES_MAX} files"
    for p in paths:
        p = p.strip()
        if not p:
            return "empty path in list"
        if p.startswith("/"):
            return f"path must be relative: {p}"
        if ".." in p.split("/"):
            return f"path must not contain '..': {p}"
    return None


def react_key(value: str) -> str | None:
    if not value:
        return "must not be empty"
    if len(value) > 64:
        return "at most 64 chars"
    return None


CHAT_TAGS = Registry(
    tags={
        "chat-send": TagSpec(
            name="chat-send",
            self_closing=False,
            attrs={"room": room, "reply_to": message_id, "files": files},
            body_max=20000,
            body_required_unless="files",
        ),
        "chat-react": TagSpec(
            name="chat-react",
            self_closing=True,
            attrs={"to": message_id, "key": react_key},
            required=frozenset({"to", "key"}),
        ),
    },
    families=("chat-",),
)


# ---- hive tags ------------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_POST_ID_RE = re.compile(r"p?([0-9]+)")
SLUG_LIST_MAX = 20
INT_LIST_MAX = 50


def slug(value: str) -> str | None:
    """A DNS-1123 label, at most 63 chars."""
    if len(value) > 63:
        return "at most 63 chars"
    if not _SLUG_RE.fullmatch(value):
        return "must be a DNS-1123 label (lowercase letters, digits, '-')"
    return None


def slug_list(value: str) -> str | None:
    items = value.split(",")
    if len(items) > SLUG_LIST_MAX:
        return f"at most {SLUG_LIST_MAX} slugs"
    for item in items:
        msg = slug(item.strip())
        if msg:
            return f"{item.strip()!r}: {msg}"
    return None


def post_id(value: str) -> str | None:
    """`p<digits>` or `<digits>`, at least 1."""
    m = _POST_ID_RE.fullmatch(value)
    if m is None or int(m.group(1)) < 1:
        return "must be a post id p<digits> or <digits>, at least 1"
    return None


def pod_or_leader(value: str) -> str | None:
    return None if value == "leader" else slug(value)


def int_list(value: str) -> str | None:
    items = value.split(",")
    if len(items) > INT_LIST_MAX:
        return f"at most {INT_LIST_MAX} ints"
    for item in items:
        item = item.strip()
        if not item.isascii() or not item.isdigit() or int(item) < 1:
            return f"{item!r}: must be a positive integer"
    return None


def lane_session(value: str) -> str | None:
    if value == "interrupt":
        return "interrupt is not allowed across sessions"
    if value not in ("regular", "idle"):
        return "must be regular or idle"
    return None


def lane_agent(value: str) -> str | None:
    if value not in ("regular", "idle", "interrupt"):
        return "must be regular, idle or interrupt"
    return None


def notify_idle(value: str) -> str | None:
    return None if value == "idle" else "only 'idle' is supported"


HIVE_TAGS = Registry(
    tags={
        "session-send": TagSpec(
            name="session-send",
            self_closing=False,
            attrs={"to": slug, "handled": int_list, "lane": lane_session},
            required=frozenset({"to"}),
            body_max=16000,
        ),
        "agent-send": TagSpec(
            name="agent-send",
            self_closing=False,
            attrs={"to": pod_or_leader, "lane": lane_agent, "notify": notify_idle},
            required=frozenset({"to"}),
            body_max=60000,
        ),
        "project-post": TagSpec(
            name="project-post",
            self_closing=False,
            attrs={
                "to": slug_list,
                "reply_to": post_id,
                "handled": int_list,
                "project": slug,
            },
            body_max=4000,
        ),
    },
    families=("session-", "agent-", "project-"),
)


def merge_registries(*registries: Registry) -> Registry:
    """Union of tags and families; a duplicate tag name is a ValueError."""
    tags: dict[str, TagSpec] = {}
    families: list[str] = []
    for reg in registries:
        for name, spec in reg.tags.items():
            if name in tags:
                raise ValueError(f"duplicate tag {name}")
            tags[name] = spec
        families.extend(f for f in reg.families if f not in families)
    return Registry(tags=tags, families=tuple(families))
