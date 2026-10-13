"""The error taxonomy shared by the runtime, the CLI and the runner protocol.

Every failure a caller can see is a PatternError with a stable `code`. The protocol
maps `exit_code` straight to the process exit status: 2 validation, 3 runtime,
4 protocol mismatch.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_RUNTIME = 3
EXIT_PROTOCOL = 4


class PatternError(Exception):
    """A failure with a stable machine-readable code."""

    code = "pattern_error"
    exit_code = EXIT_RUNTIME
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.http_status = http_status
        if retryable is not None:
            self.retryable = retryable


class ValidationError(PatternError):
    """Bad input: params, config, a custom pattern definition, or a request."""

    code = "validation_error"
    exit_code = EXIT_VALIDATION


class PatternNotFound(ValidationError):
    code = "pattern_not_found"


class TrustError(ValidationError):
    """A custom Python pattern whose bytes do not match the sha256 the caller trusted."""

    code = "trust_mismatch"


class TemplateError(ValidationError):
    code = "template_error"


class ProtocolMismatch(PatternError):
    code = "protocol_mismatch"
    exit_code = EXIT_PROTOCOL


class EgressBlocked(PatternError):
    """The egress guard refused a destination (private range, bad scheme, rebinding...)."""

    code = "egress_blocked"


class HttpError(PatternError):
    """A non-success HTTP response after retries."""

    code = "http_error"


class TransportError(PatternError):
    """Connection, TLS or timeout failure after retries."""

    code = "transport_error"
    retryable = True


class ResponseTooLarge(PatternError):
    code = "response_too_large"


class BudgetExhausted(PatternError):
    """A run-level byte or time budget ran out. The runtime turns this into truncation."""

    code = "budget_exhausted"

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
