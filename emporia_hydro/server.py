# Copyright (c) 2026 Mark Buckaway. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full text.

"""Local, on-demand static file server for the generated reports directory.

This module serves ``reports/`` (``index.html``, per-run report pages,
``static/`` assets, and chart SVGs) over plain HTTP so the dashboard can be
viewed in a browser. It binds to loopback only and is meant to be started
on-demand from the CLI's ``serve`` subcommand; there is no daemon, TLS, or
remote-access support by design (see the plan's local-server component).
"""

import functools
import http.server
import os

__all__ = ["make_server", "serve"]


def make_server(
    directory: str | os.PathLike,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> http.server.ThreadingHTTPServer:
    """Build (but do not start) a threading HTTP server rooted at ``directory``.

    Args:
        directory: Filesystem directory to serve as the HTTP document root
            (typically the generated ``reports/`` directory).
        host: Interface to bind. Defaults to the loopback address so the
            dashboard is never reachable off the local machine.
        port: TCP port to bind. Pass ``0`` to let the OS choose an ephemeral
            free port (useful for tests); the bound port is then available at
            ``server.server_address[1]``.

    Returns:
        A constructed :class:`~http.server.ThreadingHTTPServer` bound to
        ``(host, port)`` and ready to serve. The caller is responsible for
        calling ``serve_forever()`` (or ``handle_request()``) and eventually
        ``server_close()``.
    """
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    return http.server.ThreadingHTTPServer((host, port), handler)


def serve(
    directory: str | os.PathLike,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Build and run a local static file server until interrupted.

    Prints the local URL, then blocks serving requests until interrupted with
    Ctrl-C (``KeyboardInterrupt``), always closing the server socket on exit.

    Args:
        directory: Filesystem directory to serve as the HTTP document root.
        host: Interface to bind. Defaults to loopback-only.
        port: TCP port to bind. Defaults to ``8765``.
    """
    server = make_server(directory, host=host, port=port)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    print(f"Serving {directory} at http://{bound_host}:{bound_port} (Ctrl-C to stop)")
    # logic-coverage-exempt: T-15 blocking I/O loop; tests monkeypatch
    # serve_forever to exercise the interrupt/finally paths without blocking.
    try:
        server.serve_forever()  # pragma: no cover
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
