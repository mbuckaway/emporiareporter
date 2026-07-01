# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Unit tests for emporia_hydro.server - COMPLETE test suite written FIRST."""

import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from emporia_hydro.server import make_server, serve

_REQUEST_TIMEOUT_SECONDS = 5
_JOIN_TIMEOUT_SECONDS = 5


def _url(server: ThreadingHTTPServer, path: str = "/") -> str:
    """Build the loopback URL for the ephemeral port bound by ``server``."""
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}{path}"


def _run_forever(server: ThreadingHTTPServer) -> threading.Thread:
    """Start ``server.serve_forever`` on a daemon thread and return it."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _shutdown(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    """Stop a running server and join its serving thread."""
    server.shutdown()
    server.server_close()
    thread.join(timeout=_JOIN_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# make_server - construction
# ---------------------------------------------------------------------------


def test_make_server_defaulthostport_bindsloopback8765(tmp_path):
    server = make_server(tmp_path, port=0)

    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_make_server_ephemeralport_bindsnonzeroport(tmp_path):
    server = make_server(tmp_path, port=0)

    try:
        assert server.server_address[1] != 0
    finally:
        server.server_close()


def test_make_server_returnsthreadinghttpserverinstance(tmp_path):
    server = make_server(tmp_path, port=0)

    try:
        assert isinstance(server, ThreadingHTTPServer)
    finally:
        server.server_close()


def test_make_server_explicithostandport_binds(tmp_path):
    server = make_server(tmp_path, host="127.0.0.1", port=0)

    try:
        assert server.server_address == ("127.0.0.1", server.server_address[1])
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# make_server / serving - GET requests over real loopback HTTP
# ---------------------------------------------------------------------------


def test_server_getindex_returnsindexhtmlbytes(tmp_path):
    index_bytes = b"<html><body>Hello Emporia</body></html>"
    (tmp_path / "index.html").write_bytes(index_bytes)
    server = make_server(tmp_path, port=0)
    thread = _run_forever(server)

    try:
        with urllib.request.urlopen(_url(server, "/"), timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
            status = resp.status
            body = resp.read()
    finally:
        _shutdown(server, thread)

    assert (status, body) == (200, index_bytes)


def test_server_getdatafile_returnsexactbytes(tmp_path):
    data_bytes = b"day,kwh\n2026-07-01,12.5\n"
    (tmp_path / "usage.csv").write_bytes(data_bytes)
    server = make_server(tmp_path, port=0)
    thread = _run_forever(server)

    try:
        with urllib.request.urlopen(
            _url(server, "/usage.csv"), timeout=_REQUEST_TIMEOUT_SECONDS
        ) as resp:
            status = resp.status
            body = resp.read()
    finally:
        _shutdown(server, thread)

    assert (status, body) == (200, data_bytes)


def test_server_getmissingfile_raiseshttperror404(tmp_path):
    server = make_server(tmp_path, port=0)
    thread = _run_forever(server)

    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(_url(server, "/missing.html"), timeout=_REQUEST_TIMEOUT_SECONDS)
    finally:
        _shutdown(server, thread)

    assert exc_info.value.code == 404


def test_server_getnestedfile_returnsexactbytes(tmp_path):
    reports_dir = tmp_path / "static"
    reports_dir.mkdir()
    css_bytes = b"body { color: #212121; }"
    (reports_dir / "app.css").write_bytes(css_bytes)
    server = make_server(tmp_path, port=0)
    thread = _run_forever(server)

    try:
        with urllib.request.urlopen(
            _url(server, "/static/app.css"), timeout=_REQUEST_TIMEOUT_SECONDS
        ) as resp:
            status = resp.status
            body = resp.read()
    finally:
        _shutdown(server, thread)

    assert (status, body) == (200, css_bytes)


# ---------------------------------------------------------------------------
# serve() - prints URL, blocks on serve_forever, cleans up on KeyboardInterrupt
# ---------------------------------------------------------------------------


def test_serve_keyboardinterrupt_printsurlandclosescleanly(tmp_path, monkeypatch, capsys):
    created_servers: list[ThreadingHTTPServer] = []
    real_make_server = make_server

    def _fake_make_server(directory, host="127.0.0.1", port=8765):
        server = real_make_server(directory, host=host, port=port)
        created_servers.append(server)
        return server

    def _raise_keyboard_interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr("emporia_hydro.server.make_server", _fake_make_server)
    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", _raise_keyboard_interrupt)

    serve(tmp_path, host="127.0.0.1", port=0)

    server = created_servers[0]
    captured = capsys.readouterr()
    expected_url = f"http://127.0.0.1:{server.server_address[1]}"
    assert expected_url in captured.out
    with pytest.raises(OSError, match=re.escape("Bad file descriptor")):
        server.socket.getsockname()


def test_serve_normalreturn_closesserversocket(tmp_path, monkeypatch, capsys):
    created_servers: list[ThreadingHTTPServer] = []
    real_make_server = make_server

    def _fake_make_server(directory, host="127.0.0.1", port=8765):
        server = real_make_server(directory, host=host, port=port)
        created_servers.append(server)
        return server

    def _return_immediately(self):
        return None

    monkeypatch.setattr("emporia_hydro.server.make_server", _fake_make_server)
    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", _return_immediately)

    serve(tmp_path, port=0)

    server = created_servers[0]
    with pytest.raises(OSError, match=re.escape("Bad file descriptor")):
        server.socket.getsockname()
