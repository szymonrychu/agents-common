"""Async client of the agents-orchestrator service API (bearer token, service port)."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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


DOWNLOAD_MAX_BYTES = 8388608

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
    labels: list[str] = []
    project: str = "default"

    @property
    def reuse_running_agent(self) -> bool:
        return self.spec.get("reuse_running_agent") is True


class SessionSummary(_Model):
    """Readiness of a session: `ready` live ready agents, `start_state` booting, ready or
    bootstrap_failed."""

    ready: int = 0
    start_state: str | None = None


class Session(_Model):
    """An agents-orchestrator session; `status` is its status document."""

    id: uuid.UUID
    slug: str
    preset_id: uuid.UUID | None = None
    desired: str | None = None
    status: dict[str, Any] | None = None
    project: str = "default"
    summary: SessionSummary | None = None

    @property
    def is_up(self) -> bool:
        """True only for an active session whose start finished and has a ready agent; a
        missing summary (older orchestrator, list rows) means unknown, so not up."""
        return (
            self.desired == "active"
            and self.summary is not None
            and self.summary.start_state == "ready"
            and self.summary.ready > 0
        )


class Project(_Model):
    """An orchestrator project (the permission scope of presets and sessions)."""

    slug: str
    name: str
    description: str = ""
    archived: bool = False


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


class AttachmentMeta(_Model):
    """Metadata of a message attachment as the orchestrator lists it (never the bytes)."""

    name: str
    mime: str
    size: int
    path: str


@dataclass(frozen=True)
class Attachment:
    """A file sent along with a message: `data` is the raw content."""

    name: str
    mime: str
    data: bytes

    def to_json(self) -> dict[str, str]:
        return {
            "name": self.name,
            "mime": self.mime,
            "data_b64": base64.b64encode(self.data).decode("ascii"),
        }


class Message(_Model):
    id: int
    status: str
    session_id: uuid.UUID | None = None
    text: str | None = None
    turn: Turn | None = None
    command: CommandOutcome | None = None
    attachments: list[AttachmentMeta] = []


class _Accepted(_Model):
    message_id: int
    status: str = "queued"


_PRESETS = TypeAdapter(list[Preset])
_SESSIONS = TypeAdapter(list[Session])
_PROJECTS = TypeAdapter(list[Project])
_MESSAGES = TypeAdapter(list[Message])


class _Verified(_Model):
    session_id: uuid.UUID


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

    async def _request(
        self, method: str, path: str, json: Any = None, params: dict[str, str] | None = None
    ) -> Any:
        try:
            resp = await self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise OrchestratorUnavailable(f"{method} {path}: {exc!r}") from exc
        _raise_for_status(method, path, resp)
        return resp.json()

    async def list_presets(self) -> list[Preset]:
        return _PRESETS.validate_python(await self._request("GET", "/api/v1/presets"))

    async def get_preset(self, preset_id: uuid.UUID | str) -> Preset:
        return Preset.model_validate(await self._request("GET", f"/api/v1/presets/{preset_id}"))

    async def create_session(
        self, preset_id: uuid.UUID | str, slug: str, *, project: str | None = None
    ) -> Session:
        """Create a session from a preset; idempotent on slug (409 adopts the existing one).

        `project` places the session in that project (default: the preset's project). A 409
        `conflict` adopts the session of that slug only when it is in the requested project
        (any project when `project` is None); otherwise the conflict is re-raised."""
        body: dict[str, Any] = {"preset_id": str(preset_id), "slug": slug}
        if project is not None:
            body["project"] = project
        try:
            data = await self._request("POST", "/api/v1/sessions", body)
        except OrchestratorError as exc:
            if exc.status != 409 or exc.code != "conflict":
                raise
            found = await self._request("GET", "/api/v1/sessions", params={"slug": slug})
            for session in _SESSIONS.validate_python(found):
                if session.slug == slug and (project is None or session.project == project):
                    return session
            raise
        return Session.model_validate(data)

    async def list_projects(self) -> list[Project]:
        """Active projects (`GET /api/v1/projects`, readable on the service port)."""
        return _PROJECTS.validate_python(await self._request("GET", "/api/v1/projects"))

    async def get_project(self, slug: str) -> Project:
        """One project, archived or not; an unknown slug raises `NotFound`."""
        return Project.model_validate(await self._request("GET", f"/api/v1/projects/{slug}"))

    async def get_session(self, session_id: uuid.UUID | str) -> Session:
        return Session.model_validate(await self._request("GET", f"/api/v1/sessions/{session_id}"))

    async def discard_session(self, session_id: uuid.UUID | str) -> Session:
        """Start deleting a session (`DELETE /api/v1/sessions/{id}`, 202). Idempotent; an
        unknown session, or one the service token did not create, raises `NotFound`."""
        data = await self._request("DELETE", f"/api/v1/sessions/{session_id}")
        return Session.model_validate(data)

    async def verify_agent_token(self, session_id: uuid.UUID | str, token: str) -> uuid.UUID | None:
        """Check `token` against the agent token of `session_id`
        (`POST /api/v1/sessions/{id}/agent-token/verify`, service port, body-only token).
        Returns the session id the orchestrator confirmed (200), or None when it refuses (404,
        which never says why: unknown, foreign, not active or wrong token). Any other status
        raises; the token is never put in an error message."""
        try:
            data = await self._request(
                "POST", f"/api/v1/sessions/{session_id}/agent-token/verify", {"token": token}
            )
        except NotFound:
            return None
        return _Verified.model_validate(data).session_id

    async def list_sessions(self) -> list[Session]:
        return _SESSIONS.validate_python(await self._request("GET", "/api/v1/sessions"))

    async def list_messages(self, session_id: uuid.UUID | str) -> list[Message]:
        data = await self._request("GET", f"/api/v1/sessions/{session_id}/messages")
        return _MESSAGES.validate_python(data)

    async def post_message(
        self,
        session_id: uuid.UUID | str,
        text: str,
        attachments: Sequence[Attachment] | None = None,
        *,
        from_user: bool | None = None,
    ) -> Message:
        body: dict[str, Any] = {"text": text}
        if from_user is not None:
            body["from_user"] = from_user
        if attachments:
            body["attachments"] = [a.to_json() for a in attachments]
        data = await self._request("POST", f"/api/v1/sessions/{session_id}/messages", body)
        accepted = _Accepted.model_validate(data)
        return Message(
            id=accepted.message_id,
            status=accepted.status,
            session_id=uuid.UUID(str(session_id)),
            text=text,
        )

    async def download_file(
        self,
        session_id: uuid.UUID | str,
        path: str,
        agent_id: uuid.UUID | str | None = None,
        *,
        max_bytes: int = DOWNLOAD_MAX_BYTES,
    ) -> bytes:
        """Fetch a file the agent wrote in its session workdir. A file over `max_bytes` raises
        `OrchestratorError(413, "too_large")` and the rest of the body is not read."""
        url = f"/api/v1/sessions/{session_id}/files/download"
        params = {"path": path}
        if agent_id is not None:
            params["agent_id"] = str(agent_id)
        try:
            async with self._http.stream("GET", url, params=params) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                _raise_for_status("GET", url, resp)
                declared = resp.headers.get("Content-Length")
                if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                    raise _too_large(max_bytes)
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise _too_large(max_bytes)
                return bytes(body)
        except httpx.HTTPError as exc:
            raise OrchestratorUnavailable(f"GET {url}: {exc!r}") from exc

    async def get_message(self, message_id: int) -> Message:
        return Message.model_validate(await self._request("GET", f"/api/v1/messages/{message_id}"))


def _raise_for_status(method: str, path: str, resp: httpx.Response) -> None:
    if resp.status_code >= 500:
        raise OrchestratorUnavailable(f"{method} {path}: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise _error_from(resp)


def _too_large(max_bytes: int) -> OrchestratorError:
    return OrchestratorError(413, "too_large", f"file is larger than {max_bytes} bytes")


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
