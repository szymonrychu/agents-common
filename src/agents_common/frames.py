"""Builders for the `<message-in>` frames delivered to agents. Pure and stdlib-only."""

from __future__ import annotations

NOTICE_STATUSES = ("idle", "delivered", "refused", "failed")


def escape_body(s: str) -> str:
    """Escape `&`, `<` and `>`; newlines are kept."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(s: str) -> str:
    """Escape `&`, `<`, `>` and `"`."""
    return escape_body(s).replace('"', "&quot;")


def message_in(
    channel: str, sender: str, msg_id: str, body: str, trailer: str, **attrs: str | None
) -> str:
    """One frame; `attrs` follow channel, from and id in call order, None values are skipped.

    A trailing underscore is dropped from a name, so `for_` renders as `for`.
    """
    parts = [("channel", channel), ("from", sender), ("id", msg_id)]
    parts.extend((k.removesuffix("_"), v) for k, v in attrs.items() if v is not None)
    head = " ".join(f'{k}="{escape_attr(v)}"' for k, v in parts)
    return f"<message-in {head}>\n{escape_body(body)}\n</message-in>\n{trailer}\n"


def notice(sender: str, ref: str, status: str, reason: str | None = None) -> str:
    """A self-closing notice frame for a sent envelope `ref`."""
    if status not in NOTICE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(NOTICE_STATUSES)}")
    parts = [("channel", "notice"), ("from", sender), ("for", ref), ("status", status)]
    if reason is not None:
        parts.append(("reason", reason))
    head = " ".join(f'{k}="{escape_attr(v)}"' for k, v in parts)
    return f"<message-in {head}/>\n"
