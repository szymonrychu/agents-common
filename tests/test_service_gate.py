"""ServiceTokenGate: bearer service-token ASGI guard."""

from __future__ import annotations

import hmac
import logging

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from agents_common import service_gate
from agents_common.service_gate import ServiceTokenGate, load_tokens

TOKEN = "s3cret-token-aaaa"
OTHER = "s3cret-token-bbbb"


def _inner() -> Starlette:
    async def ok(request):
        return PlainTextResponse("ok")

    async def ws(websocket):
        await websocket.accept()
        await websocket.close()

    return Starlette(
        routes=[
            Route("/healthz", ok, methods=["GET", "HEAD", "POST"]),
            Route("/readyz", ok),
            Route("/admin", ok, methods=["GET", "POST"]),
            WebSocketRoute("/ws", ws),
        ]
    )


def _client(*tokens: str) -> TestClient:
    return TestClient(ServiceTokenGate(_inner(), tokens))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_valid_token_passes():
    assert _client(TOKEN).get("/admin", headers=_auth(TOKEN)).text == "ok"


def test_missing_invalid_and_empty_share_one_401_body():
    c = _client(TOKEN)
    cases = [
        {},
        _auth("wrong"),
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
    ]
    bodies = []
    for headers in cases:
        r = c.get("/admin", headers=headers)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        bodies.append(r.content)
    assert len(set(bodies)) == 1
    assert b"unauthorized" in bodies[0]


def test_multiple_accepted_tokens():
    c = _client(TOKEN, OTHER)
    assert c.get("/admin", headers=_auth(TOKEN)).status_code == 200
    assert c.get("/admin", headers=_auth(OTHER)).status_code == 200
    assert c.get("/admin", headers=_auth("nope")).status_code == 401


def test_no_tokens_rejects_everything():
    assert _client().get("/admin", headers=_auth(TOKEN)).status_code == 401


def test_empty_configured_token_rejected_at_construction():
    for bad in ("", "  ", "\t\n"):
        with pytest.raises(ValueError):
            ServiceTokenGate(_inner(), [bad])


def test_health_paths_exempt_for_get_and_head_only():
    c = _client(TOKEN)
    assert c.get("/healthz").status_code == 200
    assert c.get("/readyz").status_code == 200
    assert c.head("/healthz").status_code == 200
    assert c.post("/healthz").status_code == 401


def test_websocket_closed():
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect),
        _client(TOKEN).websocket_connect("/ws", headers=_auth(TOKEN)),
    ):
        pass


def test_compare_digest_used(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(service_gate.hmac, "compare_digest", spy)
    c = _client(TOKEN, OTHER)
    c.get("/admin", headers=_auth(TOKEN))
    assert len(calls) == 2  # every entry compared, no early exit


def test_token_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    c = _client(TOKEN)
    c.get("/admin", headers=_auth("presented-secret-xyz"))
    c.get("/admin", headers=_auth(TOKEN))
    assert "presented-secret-xyz" not in caplog.text
    assert TOKEN not in caplog.text


def test_load_tokens(tmp_path):
    f = tmp_path / "tokens"
    f.write_text(f"{TOKEN}\n\n  {OTHER}  \n")
    assert load_tokens(f) == [TOKEN, OTHER]


def test_load_tokens_empty_file_is_error(tmp_path):
    f = tmp_path / "tokens"
    f.write_text("\n \n")
    with pytest.raises(ValueError):
        load_tokens(f)


def test_load_tokens_missing_file_error_omits_contents(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_tokens(tmp_path / "absent")


def test_load_tokens_non_utf8_error_leaks_no_bytes(tmp_path):
    f = tmp_path / "tokens"
    f.write_bytes(b"good\xfe\xfdsecret\n")
    with pytest.raises(ValueError) as exc:
        load_tokens(f)
    text = f"{exc.value!s} {exc.value!r}"
    assert "xfe" not in text.lower()
    assert "secret" not in text
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__
