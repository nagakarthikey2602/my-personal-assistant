"""Audit redaction and the application-level offline network guard."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from personal_assistant.audit import AuditLogger
from personal_assistant.config import Config
from personal_assistant.errors import NetworkBlockedError
from personal_assistant.offline_guard import (
    install_network_guard,
    is_installed,
    self_test_local_socket,
    status,
    uninstall_network_guard,
)
from personal_assistant.util import redact


def test_audit_log_redacts_secrets_and_appends_jsonl(workspace: Path) -> None:
    cfg = Config.from_dict({"paths": {"write_roots": [str(workspace / "sandbox")]}}, workspace)
    audit = AuditLogger(cfg)

    audit.log("test_event", password="hunter2", token="abc123", ordinary="visible")
    audit.log("second_event", message="password=letmein and a normal note")

    lines = audit.path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert records[0]["event"] == "test_event"
    assert records[0]["password"] == "***REDACTED***"
    assert records[0]["token"] == "***REDACTED***"
    assert records[0]["ordinary"] == "visible"
    assert "letmein" not in lines[1]
    assert audit.tail(1)[0]["event"] == "second_event"


def test_redact_handles_nested_values_and_bounds_text() -> None:
    value = redact({
        "api_key": "secret-value",
        "nested": [{"Authorization": "Bearer abcdefghijklmnop"}],
        "text": "x" * 1000,
    })
    assert value["api_key"] == "***REDACTED***"
    assert "abcdefghijklmnop" not in json.dumps(value)
    assert "truncated" in value["text"]


@pytest.mark.offline
def test_network_guard_blocks_external_and_allows_loopback(no_python_network_guard) -> None:
    install_network_guard()
    assert is_installed()

    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("192.0.2.1", 9), timeout=0.2)
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(NetworkBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))

    ok, detail = self_test_local_socket()
    assert ok, detail
    assert status()["blocked_attempts"] >= 3
    assert "child processes" in status()["scope"]


@pytest.mark.offline
def test_network_guard_install_is_idempotent_and_restores(no_python_network_guard) -> None:
    original_connect = socket.socket.connect
    install_network_guard()
    guarded = socket.socket.connect
    install_network_guard()
    assert socket.socket.connect is guarded
    uninstall_network_guard()
    assert socket.socket.connect is original_connect
    assert not is_installed()
