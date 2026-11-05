"""Shared constants, the lazy ``httpx`` proxy and :class:`AuthError` for the auth package.

Pure leaf: imports nothing from ``noesia_cli.auth`` so the per-provider modules
(``auth_openrouter``, ``auth_zai_kimi``, ...) can import it at module scope without cycles."""

from __future__ import annotations

import base64
import json
from typing import Any, Callable, Dict, Optional

# httpx is imported lazily (~30ms) because noesia_cli.auth is on the interactive-CLI startup path
# (credential_pool -> auxiliary_client -> cli_commands_mixin). The proxy resolves on first attribute
# access; ``from __future__ import annotations`` keeps ``httpx.Client`` annotations unevaluated.
import importlib as _importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx
else:
    class _LazyHttpx:
        __slots__ = ("_mod",)

        def __init__(self) -> None:
            object.__setattr__(self, "_mod", None)

        def _resolve(self):
            mod = object.__getattribute__(self, "_mod")
            if mod is None:
                mod = _importlib.import_module("httpx")
                object.__setattr__(self, "_mod", mod)
            return mod

        def __getattr__(self, name):
            return getattr(self._resolve(), name)

        # set/del forward to the real module so monkeypatch.setattr("noesia_cli.auth.httpx.Client")
        # keeps working in tests.
        def __setattr__(self, name, value):
            setattr(self._resolve(), name, value)

        def __delattr__(self, name):
            delattr(self._resolve(), name)

    httpx = _LazyHttpx()

# ── Constants ───────────────────────────────────────────────────────────────────────────────────────

AUTH_STORE_VERSION = 1
AUTH_LOCK_TIMEOUT_SECONDS = 15.0

NOESIA_BILLING_MANAGE_SCOPE = "billing:manage"
NOESIA_DEVICE_CODE_SOURCE = "device_code"
NOESIA_AUTH_PATH_INVOKE_JWT = "invoke_jwt"
ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120       # refresh 2 min before expiry
NOESIA_INVOKE_JWT_MIN_TTL_SECONDS = ACCESS_TOKEN_REFRESH_SKEW_SECONDS
DEVICE_AUTH_POLL_INTERVAL_CAP_SECONDS = 1     # poll at most every 1s
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
_FORM_JSON_HEADERS = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
DEFAULT_GITHUB_MODELS_BASE_URL = "https://api.githubcopilot.com"
DEFAULT_COPILOT_ACP_BASE_URL = "acp://copilot"
DEFAULT_OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"
DEFAULT_ACTUAL_BASE_URL = "https://api.actual.inc/v1"
DEFAULT_ACTUAL_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
STEPFUN_STEP_PLAN_INTL_BASE_URL = "https://api.stepfun.ai/step_plan/v1"
STEPFUN_STEP_PLAN_CN_BASE_URL = "https://api.stepfun.com/step_plan/v1"
# OpenRouter PKCE (https://openrouter.ai/docs/guides/overview/auth/oauth): the "token" endpoint
# mints a plain user-controlled API key; there is no refresh token.
OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_AUTH_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
OPENROUTER_OAUTH_DOCS_URL = "https://openrouter.ai/docs/guides/overview/auth/oauth"

OAUTH_OVER_SSH_DOCS_URL = "https://noesia.invalid/docs/guides/oauth-over-ssh"
# LM Studio's default no-auth mode still needs *some* non-empty bearer for the API-key code paths to
# treat the provider as configured. Sent only to LM Studio, never to a remote service.
LMSTUDIO_NOAUTH_PLACEHOLDER = "dummy-lm-api-key"
ACTUAL_LOCAL_NOAUTH_PLACEHOLDER = "dummy-actual-local-api-key"

# Upstream rate-limit / usage-quota exhaustion (HTTP 429): transient, re-authenticating cannot resolve
# it, so it must stay distinct from missing/expired-credential errors.
CODEX_RATE_LIMITED_CODE = "codex_rate_limited"


class AuthError(RuntimeError):
    """Structured auth error with UX mapping hints."""

    def __init__(
        self, message: str, *, provider: str = "", code: Optional[str] = None, relogin_required: bool = False,
        retry_after: Optional[float] = None, retryable: Optional[bool] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.relogin_required = relogin_required
        # Optional wait hint in seconds (a server ``Retry-After`` or a client cooldown) and whether a
        # later attempt can succeed at all. None = the raiser did not say; callers treat None as
        # "retryable, no hint" for transport-shaped errors and as terminal for auth refusals.
        self.retry_after = retry_after
        self.retryable = retryable


def _provider_error_factory(provider: str) -> Callable[..., AuthError]:
    def factory(message: str, code: Optional[str] = None, *, relogin: bool = False) -> AuthError:
        return AuthError(message, provider=provider, code=code, relogin_required=relogin)

    return factory


# Per-provider AuthError constructors: ``_openrouter_err(message, code, relogin=True)``.
_noesia_err = _provider_error_factory("noesia")
_openrouter_err = _provider_error_factory("openrouter")


def _decode_jwt_claims(token: Any) -> dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        claims = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}
