"""Application-specific exceptions with safe, user-facing messages."""

from __future__ import annotations


class AssistantError(Exception):
    """Base class for errors that are safe to show to the user."""

    user_message = "The operation could not be completed."

    def __init__(self, message: str | None = None, *, detail: str | None = None) -> None:
        super().__init__(message or self.user_message)
        self.detail = detail


class ConfigError(AssistantError):
    user_message = "The configuration is invalid."


class PathDeniedError(AssistantError):
    user_message = "That path is outside the folders this assistant is allowed to use."


class ValidationError(AssistantError):
    user_message = "The command arguments are not valid."


class PermissionDenied(AssistantError):
    user_message = "This action requires approval that was not granted."


class ApprovalRequired(AssistantError):
    user_message = "This action needs your approval."


class SafetyHalted(AssistantError):
    user_message = "The kill switch is active. No new actions will run until it is reset."


class TaskCancelled(AssistantError):
    user_message = "The task was cancelled."


class ToolUnavailable(AssistantError):
    user_message = "That capability is not available on this computer."


class VerificationError(AssistantError):
    user_message = "The operation ran but its result could not be verified."


class NetworkBlockedError(AssistantError):
    user_message = "Network access is blocked by the assistant's offline policy."


class SecretStorageError(AssistantError):
    user_message = "The protected local secret could not be read or written."
