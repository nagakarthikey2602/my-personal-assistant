"""Kill switch, cancellation tokens, and stop hooks."""

from __future__ import annotations

import threading
from typing import Callable

from ..errors import SafetyHalted, TaskCancelled
from ..util import utc_now


class CancellationToken:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "Cancelled by user.") -> None:
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise TaskCancelled(self.reason or "The task was cancelled.")


class SafetyController:
    """Global kill switch plus per-task cancellation.

    The kill switch is runtime-only and is never persisted. It blocks all new
    tool execution and requests cancellation of existing tasks.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._halted = False
        self._reason = ""
        self._halted_at = ""
        self._tokens: set[CancellationToken] = set()
        self._stop_hooks: list[Callable[[], None]] = []

    @property
    def halted(self) -> bool:
        with self._lock:
            return self._halted

    def new_token(self, task_id: str) -> CancellationToken:
        with self._lock:
            token = CancellationToken(task_id)
            self._tokens.add(token)
            return token

    def release_token(self, token: CancellationToken) -> None:
        with self._lock:
            self._tokens.discard(token)

    def assert_ready(self, token: CancellationToken | None = None) -> None:
        with self._lock:
            halted = self._halted
            reason = self._reason
        if halted:
            raise SafetyHalted(f"The kill switch is active: {reason}")
        if token is not None:
            token.check()

    def halt(self, reason: str = "Kill switch activated by user.") -> None:
        with self._lock:
            self._halted = True
            self._reason = reason
            self._halted_at = utc_now()
            tokens = list(self._tokens)
            hooks = list(self._stop_hooks)
        for token in tokens:
            token.cancel(reason)
        for hook in hooks:
            try:
                hook()
            except Exception:
                pass

    def resume(self) -> None:
        with self._lock:
            self._halted = False
            self._reason = ""
            self._halted_at = ""

    def cancel_all(self, reason: str = "Stopped by user.") -> int:
        with self._lock:
            tokens = list(self._tokens)
        for token in tokens:
            token.cancel(reason)
        return len(tokens)

    def register_stop_hook(self, hook: Callable[[], None]) -> None:
        with self._lock:
            self._stop_hooks.append(hook)

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "halted": self._halted,
                "reason": self._reason,
                "halted_at": self._halted_at,
                "active_tasks": len(self._tokens),
            }
