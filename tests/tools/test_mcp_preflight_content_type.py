"""Tests for MCP HTTP content-type preflight fast-fail behavior."""

from __future__ import annotations

import asyncio
import http.server
import socket
import socketserver
import threading
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from tools.mcp_tool import MCPServerTask, NonMcpEndpointError


def _make_task(name: str = "probe_srv") -> MCPServerTask:
    task = MCPServerTask.__new__(MCPServerTask)
    task.name = name
    return task


@contextmanager
def _serve(handler_cls):
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _handler(
    *,
    status: int = 200,
    content_type: str | None = "text/html; charset=utf-8",
    body: bytes = b"<html>x</html>",
    head_status: int | None = None,
    record: list[tuple[str, dict[str, str]]] | None = None,
):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def _write(self, response_status: int, payload: bytes) -> None:
            self.send_response(response_status)
            if content_type is not None:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _record(self, method: str) -> None:
            if record is not None:
                record.append((method, dict(self.headers.items())))

        def do_HEAD(self) -> None:
            self._record("HEAD")
            self._write(head_status if head_status is not None else status, b"")

        def do_GET(self) -> None:
            self._record("GET")
            self._write(status, body)

        def log_message(self, format, *args):  # noqa: A002
            pass

    return _Handler


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html; charset=utf-8",
        "text/plain",
        "application/xml",
    ],
)
def test_2xx_non_mcp_content_types_fail_fast(content_type):
    task = _make_task("bad_srv")

    with _serve(_handler(content_type=content_type)) as base:
        with pytest.raises(NonMcpEndpointError) as exc_info:
            asyncio.run(task._preflight_content_type(f"{base}/mcp", timeout=5.0))

    message = str(exc_info.value)
    assert "bad_srv" in message
    assert content_type.split(";", 1)[0].lower() in message
    assert "application/json" in message
    assert "text/event-stream" in message


def test_non_mcp_endpoint_error_is_connection_error():
    assert issubclass(NonMcpEndpointError, ConnectionError)


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "application/json; charset=utf-8",
        "text/event-stream",
        "TEXT/EVENT-STREAM",
    ],
)
def test_mcp_content_types_pass(content_type):
    task = _make_task()

    with _serve(_handler(content_type=content_type, body=b"{}")) as base:
        asyncio.run(task._preflight_content_type(f"{base}/mcp", timeout=5.0))


def test_head_405_falls_back_to_get_and_rejects_html():
    task = _make_task("fallback_srv")
    record: list[tuple[str, dict[str, str]]] = []

    with _serve(
        _handler(content_type="text/html", head_status=405, record=record)
    ) as base:
        with pytest.raises(NonMcpEndpointError):
            asyncio.run(task._preflight_content_type(f"{base}/mcp", timeout=5.0))

    assert [method for method, _headers in record] == ["HEAD", "GET"]


def test_head_501_falls_back_to_get_and_allows_json():
    task = _make_task()
    record: list[tuple[str, dict[str, str]]] = []

    with _serve(
        _handler(
            content_type="application/json",
            body=b"{}",
            head_status=501,
            record=record,
        )
    ) as base:
        asyncio.run(task._preflight_content_type(f"{base}/mcp", timeout=5.0))

    assert [method for method, _headers in record] == ["HEAD", "GET"]


def test_network_error_passes_through_to_real_handshake():
    task = _make_task()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]

    asyncio.run(
        task._preflight_content_type(
            f"http://127.0.0.1:{dead_port}/mcp",
            timeout=2.0,
        )
    )


def test_headers_are_forwarded_to_head_and_get_fallback():
    task = _make_task()
    record: list[tuple[str, dict[str, str]]] = []

    with _serve(
        _handler(
            content_type="application/json",
            body=b"{}",
            head_status=405,
            record=record,
        )
    ) as base:
        asyncio.run(
            task._preflight_content_type(
                f"{base}/mcp",
                headers={"Authorization": "Bearer test", "X-MCP": "yes"},
                timeout=5.0,
            )
        )

    assert [method for method, _headers in record] == ["HEAD", "GET"]
    for _method, headers in record:
        assert headers["Authorization"] == "Bearer test"
        assert headers["X-MCP"] == "yes"


def test_ssl_verify_and_client_cert_are_forwarded(monkeypatch):
    captured: dict = {}

    import httpx

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def head(self, url, headers=None):
            return httpx.Response(200, headers={"content-type": "application/json"})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    task = _make_task()
    asyncio.run(
        task._preflight_content_type(
            "https://mcp.example.com/mcp",
            ssl_verify=False,
            client_cert="/path/to/client.pem",
            timeout=3.0,
        )
    )

    assert captured["verify"] is False
    assert captured["cert"] == "/path/to/client.pem"
    assert captured["follow_redirects"] is True


def test_run_preflights_once_and_does_not_enter_reconnect_loop_on_non_mcp_endpoint():
    server = MCPServerTask("bad_srv")
    calls = 0

    async def fake_preflight(self, url, **kwargs):
        nonlocal calls
        calls += 1
        raise NonMcpEndpointError("not an MCP endpoint")

    with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True), \
         patch.object(MCPServerTask, "_preflight_content_type", fake_preflight), \
         patch.object(MCPServerTask, "_run_http", new=AsyncMock()) as run_http:
        asyncio.run(server.run({"url": "https://example.com"}))

    assert calls == 1
    assert isinstance(server._error, NonMcpEndpointError)
    assert server._ready.is_set()
    run_http.assert_not_called()


def test_run_skips_preflight_for_explicit_sse_transport():
    server = MCPServerTask("sse_srv")

    async def fake_run_http(self, config):
        self._shutdown_event.set()

    with patch("tools.mcp_tool._MCP_HTTP_AVAILABLE", True), \
         patch.object(MCPServerTask, "_preflight_content_type", new=AsyncMock()) as preflight, \
         patch.object(MCPServerTask, "_run_http", fake_run_http):
        asyncio.run(server.run({"url": "https://example.com/sse", "transport": "sse"}))

    preflight.assert_not_called()
