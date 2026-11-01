"""Async client of the agents-orchestrator service API (bearer token, service port)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter


class OrchestratorError(Exception):
    """agents-orchestrator answered with an error body {error: {code, message}}.

    A post to a session being deleted answers 409 `session_deleting`.
    """

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code}: {message}")


class NotFound(OrchestratorError):
    """404 from agents-orchestrator."""


_RETRY_MARGIN = timedelta(seconds=60)
_RETRY_DEFAULT = timedelta(seconds=300)


class UsageQuotaRefused(OrchestratorError):
    """409 `usage_quota`: the Claude usage gate refused a new session.

    `metric` is the gating metric, `percent` its current usage, `limit` the threshold,
    `resets_at` when the window resets and `retry_after` the advised wait in seconds; each is
    None when the orchestrator did not send it.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        metric: str | None = None,
        percent: float | None = None,
        limit: int | None = None,
        resets_at: datetime | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(status, code, message)
        self.metric = metric
        self.percent = percent
        self.limit = limit
        self.resets_at = resets_at
        self.retry_after = retry_after

    def retry_at(self, now: datetime) -> datetime:
        """When to try again: the window reset (else `now` + `retry_after`, else 300 s) plus a
        60 s margin, and never earlier than `now` + 60 s."""
        if self.resets_at is not None:
            base = self.resets_at
        else:
            base = now + (
                timedelta(seconds=self.retry_after)
                if self.retry_after is not None
                else _RETRY_DEFAULT
            )
        return max(base, now) + _RETRY_MARGIN


class OrchestratorUnavailable(Exception):
    """Transport failure or 5xx; the caller may retry."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Preset(_Model):
    id: uuid.UUID
    name: str
    is_default: bool = False
    spec: dict[str, Any] = {}

    @property
    def reuse_running_agent(self) -> bool:
        return self.spec.get("reuse_running_agent") is True


class Session(_Model):
    """An agents-orchestrator session; `status` is its status document."""

    id: uuid.UUID
    slug: str
    preset_id: uuid.UUID | None = None
    desired: str | None = None
    status: dict[str, Any] | None = None


class Turn(_Model):
    status: str | None = None
    completed_at: datetime | None = None
    final_text: str | None = None


class CommandOutcome(_Model):
    """A command message's outcome: `state` is completed, failed or cancelled; a completed
    command with `result` `turn` was promoted to a turn (`turn_id`)."""

    state: str
    result: str | None = None
    error: str | None = None
    turn_id: str | None = None
    at: datetime | None = None


class Message(_Model):
    id: int
    status: str
    session_id: uuid.UUID | None = None
    text: str | None = None
    turn: Turn | None = None
    command: CommandOutcome | None = None


class _Accepted(_Model):
    message_id: int
    status: str = "queued"


_PRESETS = TypeAdapter(list[Preset])
_SESSIONS = TypeAdapter(list[Session])
_MESSAGES = TypeAdapter(list[Message])


class OrchestratorClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        try:
            resp = await self._http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise OrchestratorUnavailable(f"{method} {path}: {exc!r}") from exc
        if resp.status_code >= 500:
            raise OrchestratorUnavailable(f"{method} {path}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise _error_from(resp)
        return resp.json()

    async def list_presets(self) -> list[Preset]:
        return _PRESETS.validate_python(await self._request("GET", "/api/v1/presets"))

    async def get_preset(self, preset_id: uuid.UUID | str) -> Preset:
        return Preset.model_validate(await self._request("GET", f"/api/v1/presets/{preset_id}"))

    async def create_session(self, preset_id: uuid.UUID | str, slug: str) -> Session:
        """Create a session from a preset; idempotent on slug (409 adopts the existing one)."""
        try:
            data = await self._request(
                "POST", "/api/v1/sessions", {"preset_id": str(preset_id), "slug": slug}
            )
        except OrchestratorError as exc:
            if exc.status != 409 or exc.code != "conflict":
                raise
            existing = _SESSIONS.validate_python(await self._request("GET", "/api/v1/sessions"))
            for session in existing:
                if session.slug == slug:
                    return session
            raise
        return Session.model_validate(data)

    async def get_session(self, session_id: uuid.UUID | str) -> Session:
        return Session.model_validate(await self._request("GET", f"/api/v1/sessions/{session_id}"))

    async def list_messages(self, session_id: uuid.UUID | str) -> list[Message]:
        data = await self._request("GET", f"/api/v1/sessions/{session_id}/messages")
        return _MESSAGES.validate_python(data)

    async def post_message(self, session_id: uuid.UUID | str, text: str) -> Message:
        data = await self._request(
            "POST", f"/api/v1/sessions/{session_id}/messages", {"text": text}
        )
        accepted = _Accepted.model_validate(data)
        return Message(
            id=accepted.message_id,
            status=accepted.status,
            session_id=uuid.UUID(str(session_id)),
            text=text,
        )

    async def get_message(self, message_id: int) -> Message:
        return Message.model_validate(await self._request("GET", f"/api/v1/messages/{message_id}"))


def _error_from(resp: httpx.Response) -> OrchestratorError:
    code, message = "error", resp.text
    try:
        err = resp.json()["error"]
        code, message = str(err["code"]), str(err["message"])
    except (ValueError, KeyError, TypeError):
        pass
    if resp.status_code == 409 and code == "usage_quota":
        return _usage_quota_from(resp, message)
    cls = NotFound if resp.status_code == 404 else OrchestratorError
    return cls(resp.status_code, code, message)


def _usage_quota_from(resp: httpx.Response, message: str) -> UsageQuotaRefused:
    err = resp.json()["error"]
    usage = err.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return UsageQuotaRefused(
        resp.status_code,
        "usage_quota",
        message,
        metric=_field(str, usage.get("metric")),
        percent=_field(float, usage.get("percent")),
        limit=_field(int, usage.get("limit")),
        resets_at=_field(datetime.fromisoformat, usage.get("resets_at")),
        retry_after=_field(int, err.get("retry_after", resp.headers.get("Retry-After"))),
    )


def _field[T](parse: Callable[[Any], T], value: Any) -> T | None:
    """`parse(value)`, or None when the value is absent or malformed."""
    if value is None:
        return None
    try:
        return parse(value)
    except (TypeError, ValueError):
        return None
