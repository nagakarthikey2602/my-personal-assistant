"""Application-level network guard for strict offline runtime.

This guard patches Python socket entry points. It allows only loopback traffic so
the app's own IPC/tests can work. It is *not* an OS firewall: it does not cover
child processes or native libraries. See docs/SECURITY.md for the distinction and
the optional OS-level firewall rules described in docs/SECURITY.md.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from pathlib import Path
from typing import Any

from .errors import NetworkBlockedError

_LOCK = threading.RLock()
_INSTALLED = False
_ORIGINALS: dict[str, Any] = {}
_BLOCKED_COUNT = 0
_ALLOWED_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", ""}


def _is_loopback_host(host: Any) -> bool:
    if host is None:
        return False
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii", "strict")
        except UnicodeDecodeError:
            return False
    text = str(host).strip().strip("[]").split("%", 1)[0]
    if text.lower() in _ALLOWED_HOSTS:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _address_is_loopback(address: Any) -> bool:
    if isinstance(address, tuple) and address:
        return _is_loopback_host(address[0])
    if isinstance(address, (str, bytes)):
        return _is_loopback_host(address)
    return False


def _blocked(operation: str, target: Any) -> NetworkBlockedError:
    global _BLOCKED_COUNT
    with _LOCK:
        _BLOCKED_COUNT += 1
    return NetworkBlockedError(
        f"Blocked non-loopback network access during {operation}: {target!r}. "
        "Strict offline mode is enforced."
    )


def install_network_guard() -> None:
    """Idempotently install the socket guard."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        _ORIGINALS["connect"] = socket.socket.connect
        _ORIGINALS["connect_ex"] = socket.socket.connect_ex
        _ORIGINALS["sendto"] = socket.socket.sendto
        _ORIGINALS["bind"] = socket.socket.bind
        _ORIGINALS["getaddrinfo"] = socket.getaddrinfo
        _ORIGINALS["create_connection"] = socket.create_connection

        def guarded_connect(self: socket.socket, address: Any) -> Any:
            if self.family in (socket.AF_INET, socket.AF_INET6) and not _address_is_loopback(address):
                raise _blocked("connect", address)
            return _ORIGINALS["connect"](self, address)

        def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
            if self.family in (socket.AF_INET, socket.AF_INET6) and not _address_is_loopback(address):
                raise _blocked("connect_ex", address)
            return _ORIGINALS["connect_ex"](self, address)

        def guarded_sendto(self: socket.socket, data: bytes, *args: Any) -> Any:
            address = args[-1] if args else None
            if self.family in (socket.AF_INET, socket.AF_INET6) and not _address_is_loopback(address):
                raise _blocked("sendto", address)
            return _ORIGINALS["sendto"](self, data, *args)

        def guarded_bind(self: socket.socket, address: Any) -> Any:
            if self.family in (socket.AF_INET, socket.AF_INET6) and not _address_is_loopback(address):
                raise _blocked("bind", address)
            return _ORIGINALS["bind"](self, address)

        def guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            if host not in (None, "", b"") and not _is_loopback_host(host):
                # Numeric loopback addresses, localhost names, and local interface
                # addresses used without DNS are fine. Everything else is blocked.
                raise _blocked("getaddrinfo", host)
            return _ORIGINALS["getaddrinfo"](host, port, *args, **kwargs)

        def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
            if not _address_is_loopback(address):
                raise _blocked("create_connection", address)
            return _ORIGINALS["create_connection"](address, *args, **kwargs)

        socket.socket.connect = guarded_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
        socket.socket.sendto = guarded_sendto  # type: ignore[method-assign]
        socket.socket.bind = guarded_bind  # type: ignore[method-assign]
        socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
        socket.create_connection = guarded_create_connection  # type: ignore[assignment]
        _INSTALLED = True


def uninstall_network_guard() -> None:
    """Restore original socket functions (intended for tests)."""
    global _INSTALLED
    with _LOCK:
        if not _INSTALLED:
            return
        socket.socket.connect = _ORIGINALS["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = _ORIGINALS["connect_ex"]  # type: ignore[method-assign]
        socket.socket.sendto = _ORIGINALS["sendto"]  # type: ignore[method-assign]
        socket.socket.bind = _ORIGINALS["bind"]  # type: ignore[method-assign]
        socket.getaddrinfo = _ORIGINALS["getaddrinfo"]  # type: ignore[assignment]
        socket.create_connection = _ORIGINALS["create_connection"]  # type: ignore[assignment]
        _INSTALLED = False


def is_installed() -> bool:
    return _INSTALLED


def status() -> dict[str, Any]:
    return {
        "installed": _INSTALLED,
        "blocked_attempts": _BLOCKED_COUNT,
        "scope": "Python sockets; loopback allowed; child processes not covered",
    }


def self_test_local_socket() -> tuple[bool, str]:
    """Open and close a loopback listener without any external traffic."""
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        client.close()
        server.close()
        return True, "loopback sockets work"
    except OSError as exc:
        return False, f"loopback test failed: {exc}"


def local_only_policy_note() -> str:
    return (
        "Application-level guard: blocks Python socket traffic to non-loopback addresses. "
        "It does not control child processes, native libraries, or other applications. "
        "Use optional OS-level firewall rules for OS-level enforcement; see docs/SECURITY.md."
    )
