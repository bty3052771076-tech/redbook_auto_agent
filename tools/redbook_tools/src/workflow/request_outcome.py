"""Classify only errors whose local/remote outcome is actually known."""
from concurrent.futures import CancelledError


class CompletedModelError(RuntimeError):
    """An adapter returned a terminal model error instead of an artifact."""


class RequestNotDispatched(RuntimeError):
    """Admission failed before a vendor request could start."""


def remote_outcome_unknown(error: BaseException) -> bool:
    if isinstance(error, (ValueError, TypeError, PermissionError, CancelledError, CompletedModelError, RequestNotDispatched)):
        return False
    if str(getattr(error, "code", "")).startswith("CAPABILITY_"):
        return False
    # Explicit HTTP responses are completed requests; connection/read failures are not.
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None) or getattr(error, "status_code", None)
    if isinstance(status, int) and 400 <= status <= 599:
        return False
    return True
