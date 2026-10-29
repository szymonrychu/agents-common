"""In-memory fake of the agents-orchestrator service API, for tests and e2e."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakeOrchestrator:
    """Serves the agents-orchestrator service-port routes the client uses (presets, sessions,
    messages). `app` is an ASGI app."""

    def __init__(self, token: str = "test-token"):
        self.token = token
        self.presets: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: dict[int, dict[str, Any]] = {}
        self.requests: list[Request] = []
        self._fail: dict[str, list[int]] = {}
        self._next_message_id = 1
        self.app = Starlette(
            routes=[
                Route("/api/v1/presets", self._wrap("list_presets", self._list_presets)),
                Route("/api/v1/presets/{id}", self._wrap("get_preset", self._get_preset)),
                Route(
                    "/api/v1/sessions",
                    self._wrap("sessions", self._sessions),
                    methods=["GET", "POST"],
                ),
                Route("/api/v1/sessions/{id}", self._wrap("get_session", self._get_session)),
                Route(
                    "/api/v1/sessions/{id}/messages",
                    self._wrap("messages", self._messages),
                    methods=["GET", "POST"],
                ),
                Route("/api/v1/messages/{id}", self._wrap("get_message", self._get_message)),
            ]
        )

    # helpers -------------------------------------------------------------

    def add_preset(
        self, name: str, is_default: bool = False, spec: dict[str, Any] | None = None
    ) -> _PresetRef:
        pid = str(uuid.uuid4())
        self.presets[pid] = {
            "id": pid,
            "name": name,
            "is_default": is_default,
            "spec": dict(spec or {}),
            "created_at": _now(),
            "updated_at": _now(),
        }
        return _PresetRef(uuid.UUID(pid), name)

    def mark_sent(self, message_id: int) -> None:
        self.messages[message_id]["status"] = "sent"

    def discard_session(self, session_id: str) -> None:
        """Start deleting the session: desired becomes `deleting`, queued messages are cancelled
        and posts answer 409 `session_deleting`."""
        self.sessions[str(session_id)]["desired"] = "deleting"
        for msg in self.messages.values():
            if msg["session_id"] == str(session_id) and msg["status"] == "queued":
                msg["status"] = "cancelled"

    def remove_session(self, session_id: str) -> None:
        """The session row is gone: it and its messages answer 404."""
        sid = str(session_id)
        del self.sessions[sid]
        self.messages = {i: m for i, m in self.messages.items() if m["session_id"] != sid}

    def complete_turn(self, message_id: int, final_text: str) -> None:
        msg = self.messages[message_id]
        msg["status"] = "sent"
        msg["turn"] = {
            "status": "completed",
            "started_at": _now(),
            "completed_at": _now(),
            "final_text": final_text,
        }

    def fail_turn(self, message_id: int) -> None:
        msg = self.messages[message_id]
        msg["status"] = "failed"
        msg["error"] = "turn failed"
        msg["turn"] = None

    def complete_command(
        self, message_id: int, result: str = "ok", turn_id: str | None = None
    ) -> None:
        """Mark a command message sent with a completed outcome (result `turn` with `turn_id`
        is a command promoted to a turn)."""
        self._command_outcome(message_id, "completed", result, None, turn_id)

    def fail_command(self, message_id: int, error: str | None, state: str = "failed") -> None:
        """Mark a command message sent with a failed or cancelled outcome."""
        if state not in ("failed", "cancelled"):
            raise ValueError(f"fail_command state must be failed or cancelled, not {state!r}")
        self._command_outcome(message_id, state, None, error, None)

    def _command_outcome(
        self,
        message_id: int,
        state: str,
        result: str | None,
        error: str | None,
        turn_id: str | None,
    ) -> None:
        msg = self.messages[message_id]
        msg["status"] = "sent"
        msg["command"] = {
            "state": state,
            "result": result,
            "error": error,
            "turn_id": turn_id,
            "at": _now(),
        }

    def fail_next(self, route: str, status: int) -> None:
        """Answer the next call of `route` with `status` (route is a method name of the
        client, plus `list_sessions`/`create_session`/`list_messages`/`post_message`)."""
        self._fail.setdefault(route, []).append(status)

    # plumbing ------------------------------------------------------------

    def _wrap(self, name: str, handler):
        async def endpoint(request: Request):
            self.requests.append(request)
            if request.headers.get("authorization") != f"Bearer {self.token}":
                return _error(401, "unauthorized", "missing or invalid service token")
            route = name
            if name == "sessions":
                route = "create_session" if request.method == "POST" else "list_sessions"
            elif name == "messages":
                route = "post_message" if request.method == "POST" else "list_messages"
            queued = self._fail.get(route)
            if queued:
                status = queued.pop(0)
                codes = {409: "conflict", 404: "not_found"}
                return _error(status, codes.get(status, "internal"), f"injected {status}")
            return await handler(request)

        return endpoint

    async def _list_presets(self, request: Request):
        return JSONResponse(list(self.presets.values()))

    async def _get_preset(self, request: Request):
        preset = self.presets.get(request.path_params["id"])
        if preset is None:
            return _error(404, "not_found", "preset not found")
        return JSONResponse(preset)

    async def _sessions(self, request: Request):
        if request.method == "GET":
            return JSONResponse(list(self.sessions.values()))
        body = await request.json()
        preset_id, slug = body.get("preset_id"), body.get("slug")
        if preset_id not in self.presets:
            return _error(404, "not_found", "preset not found")
        if any(s["slug"] == slug for s in self.sessions.values()):
            return _error(409, "conflict", f"session {slug!r} already exists")
        sid = str(uuid.uuid4())
        session = {
            "id": sid,
            "slug": slug,
            "preset_id": preset_id,
            "preset_name": self.presets[preset_id]["name"],
            "desired": "active",
            "created_at": _now(),
        }
        self.sessions[sid] = session
        return JSONResponse(session, status_code=201)

    async def _get_session(self, request: Request):
        session = self.sessions.get(request.path_params["id"])
        if session is None:
            return _error(404, "not_found", "session not found")
        return JSONResponse(session)

    async def _messages(self, request: Request):
        sid = request.path_params["id"]
        if sid not in self.sessions:
            return _error(404, "not_found", "session not found")
        if request.method == "GET":
            return JSONResponse([m for m in self.messages.values() if m["session_id"] == sid])
        if self.sessions[sid]["desired"] == "deleting":
            return _error(409, "session_deleting", "session is being deleted")
        body = await request.json()
        mid = self._next_message_id
        self._next_message_id += 1
        self.messages[mid] = {
            "id": mid,
            "session_id": sid,
            "text": body["text"],
            "status": "queued",
            "error": None,
            "created_at": _now(),
            "turn": None,
            "command": None,
        }
        return JSONResponse({"message_id": mid, "status": "queued"}, status_code=202)

    async def _get_message(self, request: Request):
        try:
            msg = self.messages.get(int(request.path_params["id"]))
        except ValueError:
            msg = None
        if msg is None:
            return _error(404, "not_found", "message not found")
        return JSONResponse(msg)


class _PresetRef:
    """Returned by add_preset: the id (UUID) and name of the new preset."""

    def __init__(self, id: uuid.UUID, name: str):
        self.id = id
        self.name = name
