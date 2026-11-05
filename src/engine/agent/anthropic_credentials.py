"""Anthropic credential resolution.

``resolve_anthropic_token()`` order: ``ANTHROPIC_TOKEN`` (a token the user pasted),
``ANTHROPIC_API_KEY``, then api_key rows in the ``auth.json`` credential pool. Reads
here are read-only: diagnostic call sites must never mutate the pool or hit the
network, and refresh/rotation belongs to the API call path.
"""

import logging
from typing import Any, Optional

from utils import base_url_hostname
from agent.secret_scope import get_secret as _get_secret

logger = logging.getLogger(__name__)


def _getenv(name: str, default: str = "") -> str:
    """Profile-scoped os.getenv for credential reads (fail-closed on unscoped reads when multiplexing)."""
    val = _get_secret(name, default)
    return val if val is not None else default


def _first_env(*names: str) -> str:
    """First non-blank (stripped) value among *names*, else ''."""
    return next((v for v in (_getenv(n).strip() for n in names) if v), "")


def is_native_anthropic_host(base_url: Any) -> bool:
    """True when *base_url* is exactly ``api.anthropic.com`` (an empty base_url is the native default)."""
    text = str(base_url or "").strip()
    return not text or base_url_hostname(text) == "api.anthropic.com"


def _resolve_anthropic_pool_token() -> Optional[str]:
    """First available Anthropic api_key from credential_pool, read-only: enumerates with
    ``clear_expired=False`` (never ``select()``) so diagnostic call sites (account_usage,
    ``noesia models``) never mutate auth.json or hit the network."""
    try:
        from agent.credential_pool import AUTH_TYPE_API_KEY, load_pool
        entries = load_pool("anthropic")._available_entries(clear_expired=False)
    except Exception:
        logger.debug("Failed to read Anthropic credential_pool", exc_info=True)
        return None
    for entry in entries:
        # access_token may be an explicit null on a persisted entry; None.strip() would crash the resolver.
        token = (getattr(entry, "access_token", None) or "").strip()
        if getattr(entry, "auth_type", None) != AUTH_TYPE_API_KEY or not token:
            continue
        return token
    return None


def _available_anthropic_token(token: Optional[str], model: Optional[str]) -> Optional[str]:
    """Return *token* unless the pool holds an active cooldown for it on *model*.

    Only model-aware callers (the API-call paths) are gated: diagnostics that
    resolve a token without a model (usage display, model discovery) keep it.
    """
    if not token or not model:
        return token or None
    try:
        from agent.credential_pool import load_pool
        if load_pool("anthropic").token_is_blocked(token, model=model):
            return None
    except Exception:
        # Credential discovery must remain available when the pool store is
        # unavailable or malformed.
        logger.debug("Failed to check Anthropic model cooldown", exc_info=True)
    return token


def resolve_anthropic_token(*, model: Optional[str] = None) -> Optional[str]:
    """Resolve an Anthropic token from env vars, then the credential pool.

    With *model*, a token the credential pool has benched for that model resolves to ``None``
    instead of being handed straight back to the caller that just saw it rate-limited.
    """
    token = _first_env("ANTHROPIC_TOKEN")
    if token:
        return _available_anthropic_token(token, model)
    api_key = _first_env("ANTHROPIC_API_KEY")  # an explicit API key must not be shadowed by pool rows
    if api_key:
        return _available_anthropic_token(api_key, model)
    return _available_anthropic_token(_resolve_anthropic_pool_token(), model)
