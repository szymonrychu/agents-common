"""In-memory fake of the orchestrator's service API, for tests and e2e."""

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
    """Serves the service-port routes the client uses. `app` is an ASGI app."""

    def __init__(self, token: str = "test-token"):
        self.token = token
        self.presets: dict[str, dict[str, Any]] = {}
        self.clusters: dict[str, dict[str, Any]] = {}
        self.messages: dict[int, dict[str, Any]] = {}
        self.requests: list[Request] = []
        self._fail: dict[str, list[int]] = {}
        self._next_message_id = 1
        self.app = Starlette(
            routes=[
                Route("/api/v1/presets", self._wrap("list_presets", self._list_presets)),
                Route("/api/v1/presets/{id}", self._wrap("get_preset", self._get_preset)),
                Route(
                    "/api/v1/clusters",
                    self._wrap("clusters", self._clusters),
                    methods=["GET", "POST"],
                ),
                Route("/api/v1/clusters/{id}", self._wrap("get_cluster", self._get_cluster)),
                Route(
                    "/api/v1/clusters/{id}/messages",
                    self._wrap("messages", self._messages),
                    methods=["GET", "POST"],
                ),
                Route("/api/v1/messages/{id}", self._wrap("get_message", self._get_message)),
            ]
        )

    # helpers -------------------------------------------------------------

    def add_preset(self, name: str, is_default: bool = False) -> _PresetRef:
        pid = str(uuid.uuid4())
        self.presets[pid] = {
            "id": pid,
            "name": name,
            "is_default": is_default,
            "spec": {},
            "created_at": _now(),
            "updated_at": _now(),
        }
        return _PresetRef(uuid.UUID(pid), name)

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

    def fail_next(self, route: str, status: int) -> None:
        """Answer the next call of `route` with `status` (route is a method name of the
        client, plus `list_clusters`/`create_cluster`/`list_messages`/`post_message`)."""
        self._fail.setdefault(route, []).append(status)

    # plumbing ------------------------------------------------------------

    def _wrap(self, name: str, handler):
        async def endpoint(request: Request):
            self.requests.append(request)
            if request.headers.get("authorization") != f"Bearer {self.token}":
                return _error(401, "unauthorized", "missing or invalid service token")
            route = name
            if name == "clusters":
                route = "create_cluster" if request.method == "POST" else "list_clusters"
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

    async def _clusters(self, request: Request):
        if request.method == "GET":
            return JSONResponse(list(self.clusters.values()))
        body = await request.json()
        preset_id, slug = body.get("preset_id"), body.get("slug")
        if preset_id not in self.presets:
            return _error(404, "not_found", "preset not found")
        if any(c["slug"] == slug for c in self.clusters.values()):
            return _error(409, "conflict", f"cluster {slug!r} already exists")
        cid = str(uuid.uuid4())
        cluster = {
            "id": cid,
            "slug": slug,
            "preset_id": preset_id,
            "preset_name": self.presets[preset_id]["name"],
            "desired": "active",
            "created_at": _now(),
        }
        self.clusters[cid] = cluster
        return JSONResponse(cluster, status_code=201)

    async def _get_cluster(self, request: Request):
        cluster = self.clusters.get(request.path_params["id"])
        if cluster is None:
            return _error(404, "not_found", "cluster not found")
        return JSONResponse(cluster)

    async def _messages(self, request: Request):
        cid = request.path_params["id"]
        if cid not in self.clusters:
            return _error(404, "not_found", "cluster not found")
        if request.method == "GET":
            return JSONResponse([m for m in self.messages.values() if m["cluster_id"] == cid])
        body = await request.json()
        mid = self._next_message_id
        self._next_message_id += 1
        self.messages[mid] = {
            "id": mid,
            "cluster_id": cid,
            "text": body["text"],
            "status": "queued",
            "error": None,
            "created_at": _now(),
            "turn": None,
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
