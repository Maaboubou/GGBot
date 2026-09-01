"""Loopback proxy that pins browser connections to pre-validated public IPs."""

from __future__ import annotations

import logging
import select
import socket
import socketserver
import threading
import time
from typing import Callable, Dict, Optional, Sequence
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)

_MAX_REQUEST_LINE = 8192
_MAX_HEADER_LINE = 16384
_MAX_HEADER_BYTES = 64 * 1024
_MAX_HEADERS = 100
_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class SafeBrowserProxyError(RuntimeError):
    """A request could not be forwarded without weakening the browser policy."""


class _ProxyTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, owner: "PinnedPublicProxy") -> None:
        self.owner = owner
        super().__init__(("127.0.0.1", 0), _ProxyHandler)


class _ProxyHandler(socketserver.StreamRequestHandler):
    def _send_response(self, status: str, *, authenticate: bool = False) -> None:
        headers = [
            f"HTTP/1.1 {status}",
            "Content-Length: 0",
            "Connection: close",
        ]
        if authenticate:
            headers.append('Proxy-Authenticate: Basic realm="wx_browser"')
        self.connection.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))

    def _read_headers(self) -> list[tuple[str, str]]:
        headers: list[tuple[str, str]] = []
        total = 0
        for _ in range(_MAX_HEADERS + 1):
            line = self.rfile.readline(_MAX_HEADER_LINE + 1)
            if not line or line in {b"\r\n", b"\n"}:
                return headers
            total += len(line)
            if len(line) > _MAX_HEADER_LINE or total > _MAX_HEADER_BYTES:
                raise SafeBrowserProxyError("proxy request headers are too large")
            try:
                decoded = line.decode("iso-8859-1").rstrip("\r\n")
                name, value = decoded.split(":", 1)
            except (UnicodeDecodeError, ValueError) as exc:
                raise SafeBrowserProxyError("proxy request header is malformed") from exc
            if not name.strip() or any(char.isspace() for char in name):
                raise SafeBrowserProxyError("proxy request header name is malformed")
            headers.append((name.strip(), value.strip()))
        raise SafeBrowserProxyError("proxy request has too many headers")

    def handle(self) -> None:
        owner = self.server.owner  # type: ignore[attr-defined]
        self.connection.settimeout(owner.client_timeout_seconds)
        target = ""
        try:
            line = self.rfile.readline(_MAX_REQUEST_LINE + 1)
            if not line:
                return
            if len(line) > _MAX_REQUEST_LINE:
                raise SafeBrowserProxyError("proxy request line is too large")
            try:
                method, target, version = line.decode("ascii").strip().split(" ", 2)
            except (UnicodeDecodeError, ValueError) as exc:
                raise SafeBrowserProxyError("proxy request line is malformed") from exc
            method = method.upper()
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise SafeBrowserProxyError("proxy HTTP version is unsupported")
            headers = self._read_headers()
            if method == "CONNECT":
                owner.forward_connect(self.connection, target)
                return
            if method not in _READ_ONLY_METHODS:
                raise SafeBrowserProxyError(f"proxy request method is blocked: {method}")
            owner.forward_http(self.connection, method, target, version, headers)
        except SafeBrowserProxyError as exc:
            owner.record_blocked(target, str(exc))
            try:
                self._send_response("403 Forbidden")
            except OSError:
                pass
        except (OSError, TimeoutError) as exc:
            owner.record_blocked(target, f"public upstream connection failed: {exc}")
            try:
                self._send_response("502 Bad Gateway")
            except OSError:
                pass
        except Exception:
            owner.record_blocked(target, "public proxy failed unexpectedly")
            logger.exception("Unexpected public browser proxy failure")
            try:
                self._send_response("502 Bad Gateway")
            except OSError:
                pass


class PinnedPublicProxy:
    """Force Chromium through a proxy that connects only to validated numeric IPs.

    URL interception remains useful for method and file-scope checks. This proxy
    closes the DNS-rebinding gap: Chromium never resolves an HTTP(S) destination
    itself, and each upstream socket is opened to the exact public IP returned by
    the policy validator.
    """

    def __init__(
        self,
        resolver: Callable[[str, int], Sequence[str]],
        *,
        connect_timeout_seconds: float = 15.0,
        client_timeout_seconds: float = 120.0,
    ) -> None:
        self._resolver = resolver
        self.connect_timeout_seconds = max(1.0, float(connect_timeout_seconds))
        self.client_timeout_seconds = max(5.0, float(client_timeout_seconds))
        self._lock = threading.Lock()
        self._blocked: list[Dict[str, str]] = []
        self._pinned_connect_count = 0
        self._server: Optional[_ProxyTCPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def playwright_settings(self) -> Dict[str, str]:
        if self._server is None:
            raise SafeBrowserProxyError("public browser proxy is not running")
        port = int(self._server.server_address[1])
        return {
            "server": f"http://127.0.0.1:{port}",
            # Chromium otherwise has an implicit loopback bypass. The route
            # policy blocks loopback too, but forcing it through this proxy is
            # an additional fail-closed layer.
            "bypass": "<-loopback>",
        }

    @property
    def stats(self) -> Dict[str, object]:
        with self._lock:
            return {
                "pinned_connect_count": self._pinned_connect_count,
                "blocked_request_count": len(self._blocked),
                "blocked_requests": list(self._blocked),
            }

    def __enter__(self) -> "PinnedPublicProxy":
        if self._server is not None:
            raise SafeBrowserProxyError("public browser proxy is already running")
        self._server = _ProxyTCPServer(self)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="codex-public-browser-proxy",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)

    def record_blocked(self, target: str, reason: str) -> None:
        with self._lock:
            if len(self._blocked) < 100:
                self._blocked.append(
                    {"target": str(target or "")[:1000], "reason": str(reason)[:500]}
                )

    @staticmethod
    def _parse_authority(authority: str, default_port: int) -> tuple[str, int]:
        try:
            parsed = urlsplit(f"//{authority}")
            host = str(parsed.hostname or "").strip().rstrip(".")
            port = parsed.port or default_port
        except ValueError as exc:
            raise SafeBrowserProxyError("proxy target authority is invalid") from exc
        if not host or parsed.username is not None or parsed.password is not None:
            raise SafeBrowserProxyError("proxy target authority is invalid")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise SafeBrowserProxyError("proxy CONNECT target is invalid")
        return host, port

    def _dial_public(self, host: str, port: int) -> socket.socket:
        try:
            addresses = tuple(self._resolver(host, port))
        except SafeBrowserProxyError:
            raise
        except Exception as exc:
            raise SafeBrowserProxyError(str(exc) or "public target validation failed") from exc
        if not addresses:
            raise SafeBrowserProxyError("public target returned no validated addresses")
        last_error: Optional[OSError] = None
        for address in addresses:
            try:
                upstream = socket.create_connection(
                    (str(address), int(port)),
                    timeout=self.connect_timeout_seconds,
                )
                upstream.settimeout(self.client_timeout_seconds)
                with self._lock:
                    self._pinned_connect_count += 1
                return upstream
            except OSError as exc:
                last_error = exc
        raise SafeBrowserProxyError("could not connect to a validated public address") from last_error

    def forward_connect(self, client: socket.socket, authority: str) -> None:
        host, port = self._parse_authority(authority, 443)
        upstream = self._dial_public(host, port)
        try:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            try:
                self._relay_bidirectional(client, upstream)
            except (OSError, TimeoutError):
                # The tunnel is already established, so the only valid
                # response to a peer disconnect is to close it quietly.
                pass
        finally:
            upstream.close()

    def forward_http(
        self,
        client: socket.socket,
        method: str,
        target: str,
        version: str,
        headers: Sequence[tuple[str, str]],
    ) -> None:
        parsed = urlsplit(target)
        if parsed.scheme.casefold() != "http" or not parsed.hostname:
            raise SafeBrowserProxyError("plain HTTP proxy target must be an absolute URL")
        if parsed.username is not None or parsed.password is not None:
            raise SafeBrowserProxyError("proxy target credentials are blocked")
        try:
            port = parsed.port or 80
        except ValueError as exc:
            raise SafeBrowserProxyError("proxy target port is invalid") from exc
        host = parsed.hostname.rstrip(".")
        upstream = self._dial_public(host, port)
        try:
            lower_headers = {name.casefold(): value for name, value in headers}
            if lower_headers.get("transfer-encoding") or lower_headers.get("content-length", "0") not in {
                "",
                "0",
            }:
                raise SafeBrowserProxyError("request bodies are blocked by the public proxy")
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            host_header = f"[{host}]" if ":" in host else host
            if port != 80:
                host_header += f":{port}"
            outgoing = [f"{method} {path} {version}", f"Host: {host_header}"]
            blocked_headers = {
                "connection",
                "host",
                "proxy-authorization",
                "proxy-connection",
                "transfer-encoding",
            }
            outgoing.extend(
                f"{name}: {value}"
                for name, value in headers
                if name.casefold() not in blocked_headers
            )
            outgoing.extend(("Connection: close", "", ""))
            upstream.sendall("\r\n".join(outgoing).encode("iso-8859-1"))
            self._relay_one_way(upstream, client)
        finally:
            upstream.close()

    def _relay_bidirectional(self, client: socket.socket, upstream: socket.socket) -> None:
        sockets = [client, upstream]
        idle_deadline = time.monotonic() + self.client_timeout_seconds
        while True:
            remaining = idle_deadline - time.monotonic()
            if remaining <= 0:
                return
            readable, _, exceptional = select.select(
                sockets,
                [],
                sockets,
                min(1.0, remaining),
            )
            if exceptional:
                return
            if not readable:
                continue
            for source in readable:
                data = source.recv(64 * 1024)
                if not data:
                    return
                destination = upstream if source is client else client
                destination.sendall(data)
                idle_deadline = time.monotonic() + self.client_timeout_seconds

    @staticmethod
    def _relay_one_way(source: socket.socket, destination: socket.socket) -> None:
        while True:
            data = source.recv(64 * 1024)
            if not data:
                return
            destination.sendall(data)
