"""Exception types shared across the triage package."""

from __future__ import annotations


class TriageError(Exception):
    """Base class for all triage errors."""


class PathViolation(TriageError):
    """A file path escaped its allowed base directory or targeted a forbidden one."""


class UnknownIncident(TriageError):
    """An incident ID is malformed or not registered in the manifest."""


class ChecksumMismatch(TriageError):
    """A fixture file's SHA-256 does not match the manifest."""


class RunbookError(TriageError):
    """A runbook file is malformed or the runbook corpus is invalid."""


class ConfigError(TriageError):
    """Required configuration is missing or invalid."""


class TransientLLMError(TriageError):
    """The LLM call timed out, could not connect, or hit a server-side error. Retryable."""


class SchemaError(TriageError):
    """Model output is not exactly one valid JSON action."""
