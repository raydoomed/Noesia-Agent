"""Plain-language copy for sign-in and provider-setup failures (CLI).

One table of ``(predicate, lead sentence)`` classifies an exception into a sentence a first-time
user can act on; the raw exception is demoted to a ``Details:`` line so nothing is lost for
support. Callers print ``provider_setup_failure_lines(...)`` line by line.
"""

from __future__ import annotations

from typing import Callable, Sequence, Tuple

# httpx class names and stdlib bases that mean "the request never got a usable answer".
_NETWORK_ERROR_TYPES = frozenset({
    "ConnectError", "ConnectTimeout", "ReadTimeout", "PoolTimeout", "WriteTimeout", "TimeoutException",
    "RemoteProtocolError", "ReadError", "ProxyError", "UnsupportedProtocol", "NetworkError",
})

class DeviceCodeExpired(TimeoutError):
    """The device code ran out before the user approved it in the browser (not a network timeout)."""

    code = "device_code_timeout"


# ``AuthError.code`` values the device-code pollers raise when the code runs out.
_DEVICE_CODE_EXPIRED_CODES = frozenset({"device_code_timeout", "timeout"})


class SignInCopyError(RuntimeError):
    """Exception whose ``str()`` is already user copy (lead line + ``Details:`` line)."""

    def __init__(self, message: str, *, oauth_error_code: str = "") -> None:
        super().__init__(message)
        self.oauth_error_code = oauth_error_code


def is_network_error(exc: BaseException) -> bool:
    """True for connection/DNS/timeout failures from httpx, requests or the stdlib."""
    if isinstance(exc, (SignInCopyError, DeviceCodeExpired)):
        return False
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & _NETWORK_ERROR_TYPES) or isinstance(exc, (ConnectionError, TimeoutError))


def is_device_code_expired(exc: BaseException) -> bool:
    """True when a device-code sign-in ended because its code ran out (any provider)."""
    return (getattr(exc, "code", None) in _DEVICE_CODE_EXPIRED_CODES
            or getattr(exc, "oauth_error_code", "") == "expired_token")


def is_cancelled(exc: BaseException) -> bool:
    return isinstance(exc, (KeyboardInterrupt, EOFError)) or (
        isinstance(exc, SystemExit) and exc.code in (130, None, 0))


def _details_line(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return f"  Details: {text}"


_Rule = tuple[Callable[[BaseException], bool], str]


def _classify(exc: BaseException, rules: Sequence[_Rule], other: str) -> str:
    return next((copy for pred, copy in rules if pred(exc)), other)


def provider_setup_failure_lines(exc: BaseException, *, retry_command: str = "noesia model") -> list:
    """Lines to print when the setup wizard's provider step fails: reason, that nothing was saved,
    and how to retry."""
    nothing_saved = (
        "Your provider settings were not changed. Continue the wizard now and run "
        f"`{retry_command}` afterwards to try again.")
    if isinstance(exc, SignInCopyError):
        lead, *details = str(exc).splitlines()
        return [f"Could not finish connecting a provider: {lead[0].lower()}{lead[1:]}", nothing_saved, *details]
    rules: Sequence[_Rule] = (
        (is_cancelled, "sign-in was cancelled"),
        (is_network_error, "no internet connection, or the provider could not be reached"),
    )
    reason = _classify(exc, rules, "something went wrong while talking to the provider")
    lines = [f"Could not finish connecting a provider ({reason}).", nothing_saved]
    if not is_cancelled(exc):
        lines.append(_details_line(exc))
    return lines
