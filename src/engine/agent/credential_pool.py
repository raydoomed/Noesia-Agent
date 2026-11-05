"""Persistent multi-credential pool for same-provider failover."""

from __future__ import annotations

from agent.credential_pool_admin import CredentialPoolAdminMixin
from agent.credential_pool_model_cooldowns import CredentialPoolModelCooldownMixin, model_cooldown_until
from agent.credential_pool_timestamps import _parse_absolute_timestamp

import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass, fields, replace
from typing import Any, Callable, Iterable, Optional

from noesia_constants import OPENROUTER_BASE_URL
from noesia_cli.config import load_env
from agent.secret_scope import get_secret as _get_secret
from agent.retry_utils import reset_delay_from_message
from agent.credential_persistence import (
    fingerprint_secret_value,
    sanitize_borrowed_credential_payload,
)
from noesia_cli.auth import (
    PROVIDER_REGISTRY,
    _decode_jwt_claims,
    _resolve_kimi_base_url,
    _resolve_zai_base_url,
    read_credential_pool,
    write_credential_pool,
)

logger = logging.getLogger(__name__)


def _load_config_safe() -> Optional[dict]:
    """Read-only config (``load_config_readonly`` skips the per-row deepcopy), None on any error.
    Inside ``frozen_runtime_config`` it is the session's frozen config: a key rotated in config.yaml
    must never seed a frozen session's pool."""
    try:
        from noesia_cli.config import load_config_readonly
        from noesia_cli.runtime_provider import _FROZEN_RUNTIME_CONFIG
        return _FROZEN_RUNTIME_CONFIG.get() or load_config_readonly()
    except Exception:
        return None


def _is_source_suppressed_fn() -> Callable[[str, str], bool]:
    """``noesia_cli.auth.is_source_suppressed`` (late-bound), or an always-False stub."""
    try:
        from noesia_cli.auth import is_source_suppressed
        return is_source_suppressed
    except ImportError:
        return lambda _p, _s: False


# --- Status and type constants ---

STATUS_OK = "ok"
STATUS_EXHAUSTED = "exhausted"
# Terminal failure — the credential can never recover on its own. DEAD entries are
# excluded from rotation unconditionally and only clear when the user replaces the key.
STATUS_DEAD = "dead"

# DEAD ``manual:*`` entries are pruned after this quiet window — the user can
# re-add the key via ``noesia auth add``.
DEAD_MANUAL_PRUNE_TTL_SECONDS = 24 * 60 * 60

AUTH_TYPE_API_KEY = "api_key"
#: 插件 provider 的 OAuth 凭据标记：token 由插件的 auth_handler 取得，池只负责存取与轮换
AUTH_TYPE_OAUTH = "oauth"

SOURCE_MANUAL = "manual"

STRATEGY_FILL_FIRST = "fill_first"
STRATEGY_ROUND_ROBIN = "round_robin"
STRATEGY_RANDOM = "random"
STRATEGY_LEAST_USED = "least_used"
SUPPORTED_POOL_STRATEGIES = {
    STRATEGY_FILL_FIRST,
    STRATEGY_ROUND_ROBIN,
    STRATEGY_RANDOM,
    STRATEGY_LEAST_USED,
}

# Cooldowns before retrying an exhausted credential. Transient 401s cool down
# briefly so single-key setups recover; 429/402/other take an hour.
# Provider-supplied reset_at timestamps override these defaults.
EXHAUSTED_TTL_401_SECONDS = 5 * 60
EXHAUSTED_TTL_429_SECONDS = 60 * 60
EXHAUSTED_TTL_DEFAULT_SECONDS = 60 * 60
# When the offending key is the sole non-DEAD entry, an hour-long bench means
# an hour of hard failures. Throttles (429/403/5xx) reset in seconds, so a sole
# credential cools down briefly instead.
EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS = 60

# ``FailoverReason.billing`` as a bare string: the pool persists classified
# failure semantics to JSON and must not import the classifier.
FAILURE_REASON_BILLING = "billing"

# Billing verdict resting on an ambiguous body (#82154): Anthropic's "out of
# extra usage" 400 is returned both for genuine overage and for a server-side
# content-filter rejection, which leaves the credential healthy. Unverified
# billing gets the short transient cooldown; genuine depletion re-latches.
FAILURE_REASON_BILLING_UNVERIFIED = "billing_unverified"

# Throttle window for the "no available entries" INFO line. Selection runs on
# every model call; on Windows several processes share one rotating log behind
# a cross-process lock, and per-selection logging stormed that lock, pegged a
# core, and stalled the event loop (Desktop backend readiness timeouts).
# Credential selection runs on a hot path (every model call, plus auxiliary tasks like
# compression/moa/titles), so when a pool is empty or fully exhausted the un-throttled log fires on *every*
# selection. On Windows several Noesia processes share one rotating log guarded by concurrent-log-handler's
# cross-process lock; that per-selection volume storms the lock (``RuntimeError: Cannot acquire lock after
# 20 attempts``), pegs a core, and stalls the asyncio event loop long enough to fail the Desktop backend
# readiness handshake ("Timed out connecting to Noesia backend after 15000ms"). Logging the condition at
# most once per window preserves the signal while removing the storm — same class of fix as the warn-once
# dedup in #58265.
NO_AVAILABLE_ENTRIES_LOG_THROTTLE_SECONDS = 60.0

# Pool key prefix for custom OpenAI-compatible endpoints: all share
# provider='custom' but are keyed 'custom:<normalized_name>'.
CUSTOM_POOL_PREFIX = "custom:"

# Fields only round-tripped through JSON — never used for logic as attributes.
_EXTRA_KEYS = frozenset({
    "token_type", "scope", "client_id", "portal_base_url", "obtained_at",
    "expires_in", "agent_key_id", "agent_key_expires_in", "agent_key_reused",
    "agent_key_obtained_at", "tls", "secret_source", "secret_fingerprint",
    # Noesia guest identity (``auth_method: anonymous``): the anon_ credential is the refresh material.
    "auth_method", "account_tier", "anon_token", "user_id", "org_id",
    # Classified failure semantics for the last exhaustion (agent/error_classifier.py).
    # Providers return 403 for both an edge throttle and a spending limit, so the
    # raw status cannot size a cooldown; persisted so a restart doesn't downgrade
    # a billing bench to a 60s transient cooldown.
    "failure_reason",
})

# ``replace(entry, **_CLEAR_STATUS)`` returns an entry with no error state.
_CLEAR_STATUS: dict[str, Any] = {
    "last_status": None,
    "last_status_at": None,
    "last_error_code": None,
    "last_error_reason": None,
    "last_error_message": None,
    "last_error_reset_at": None,
}
_MARK_OK: dict[str, Any] = {**_CLEAR_STATUS, "last_status": STATUS_OK}


@dataclass
class PooledCredential:
    provider: str
    id: str
    label: str
    auth_type: str
    priority: int
    source: str
    access_token: str
    refresh_token: Optional[str] = None
    last_status: Optional[str] = None
    last_status_at: Optional[float] = None
    last_error_code: Optional[int] = None
    last_error_reason: Optional[str] = None
    last_error_message: Optional[str] = None
    last_error_reset_at: Optional[float] = None
    # Epoch of the last deliberate ``noesia auth reset`` of this entry. Sticky: a later exhaustion
    # stamps a newer ``last_status_at``, so "reset postdates status" stays decidable across processes.
    status_cleared_at: Optional[float] = None
    base_url: Optional[str] = None
    expires_at: Optional[str] = None
    expires_at_ms: Optional[int] = None
    last_refresh: Optional[str] = None
    inference_base_url: Optional[str] = None
    agent_key: Optional[str] = None
    agent_key_expires_at: Optional[str] = None
    request_count: int = 0
    # A provider may rate-limit one model while the same credential remains
    # usable for its sibling models.  Keep that observation separate from the
    # credential-wide status used for auth and billing failures.
    model_cooldowns: Optional[dict[str, float]] = None
    extra: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}

    def __getattr__(self, name: str):
        if name in _EXTRA_KEYS:
            return self.extra.get(name)
        raise AttributeError(f"'{type(self).__name__}' object has no attribute {name!r}")

    @classmethod
    def from_dict(cls, provider: str, payload: dict[str, Any]) -> PooledCredential:
        field_names = {f.name for f in fields(cls) if f.name != "provider"}
        data = {k: payload.get(k) for k in field_names if k in payload}
        # Rehydrated last_status_at may be an ISO string from to_dict() — normalize to float epoch
        if isinstance(data.get("last_status_at"), str):
            data["last_status_at"] = _parse_absolute_timestamp(data["last_status_at"])
        # Every non-field key rides in ``extra`` (to_dict writes them all back), so metadata a plugin
        # stores on its own rows survives load -> save -> load. ``_EXTRA_KEYS`` stays the attribute
        # surface for core logic; unknown keys are opaque payload. ``provider`` is the row's owner
        # (excluded from ``field_names`` above), never metadata — sweeping it in would write a
        # stray provider name back over the row on to_dict().
        data["extra"] = {
            k: v for k, v in payload.items() if k not in field_names and k != "provider" and v is not None
        }
        data.setdefault("id", uuid.uuid4().hex[:6])
        data.setdefault("label", payload.get("source", provider))
        data.setdefault("auth_type", AUTH_TYPE_API_KEY)
        data.setdefault("priority", 0)
        data.setdefault("source", SOURCE_MANUAL)
        data.setdefault("access_token", "")
        return cls(provider=provider, **data)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_def in fields(self):
            if field_def.name in {"provider", "extra"}:
                continue
            value = getattr(self, field_def.name)
            if value is not None or field_def.name in _CLEAR_STATUS:
                result[field_def.name] = value
        for k, v in self.extra.items():
            if v is not None:
                result[k] = v
        return sanitize_borrowed_credential_payload(result)

    @property
    def runtime_api_key(self) -> str:
        return str(self.access_token or "")

    @property
    def runtime_base_url(self) -> Optional[str]:
        return self.base_url


def label_from_token(token: str, fallback: str) -> str:
    claims = _decode_jwt_claims(token)
    for key in ("email", "preferred_username", "upn"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _codex_principal_identity(access_token: Any) -> Optional[tuple[str, str]]:
    """``(chatgpt_account_id, sub)`` of a Codex access token, or None when either claim is missing.

    Decoded without signature verification: this only decides whether two credentials Noesia
    already holds belong to the same principal, never whether a token is valid. Both claims are
    required because members of one ChatGPT workspace share ``chatgpt_account_id`` yet have their
    own subjects and quotas.
    """
    claims = _decode_jwt_claims(access_token)
    auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else None
    account_id = auth_claims.get("chatgpt_account_id") if isinstance(auth_claims, dict) else None
    subject = claims.get("sub") if isinstance(claims, dict) else None
    if not (isinstance(account_id, str) and account_id.strip() and isinstance(subject, str) and subject.strip()):
        return None
    return account_id.strip(), subject.strip()


def _next_priority(entries: list[PooledCredential]) -> int:
    return max((entry.priority for entry in entries), default=-1) + 1


def _is_manual_source(source: str) -> bool:
    normalized = (source or "").strip().lower()
    return normalized == SOURCE_MANUAL or normalized.startswith(f"{SOURCE_MANUAL}:")


def _exhausted_ttl(
    error_code: Optional[int],
    *,
    sole_credential: bool = False,
    failure_reason: Optional[str] = None,
) -> int:
    """Return cooldown seconds based on the HTTP status that caused exhaustion.

    *sole_credential*: the pool has nothing to rotate to, so transient
    throttles (429 and the catch-all default covering 403/5xx/unknown) are
    capped to a brief cooldown; 401 keeps its own already-short TTL.

    *failure_reason* is the classifier verdict: an OpenRouter ``key limit
    exceeded`` and an xAI spending block both arrive as 403 but are billing,
    and a 60s retry on a spent account just re-fails. Billing keeps the full
    bench regardless of status; 402 is billing by definition.
    Unverified billing (#82154) gets the short cooldown regardless of pool
    size (the credential may be healthy), unless the status is a true 402.
    """
    if error_code == 401:
        return EXHAUSTED_TTL_401_SECONDS
    base = EXHAUSTED_TTL_429_SECONDS if error_code == 429 else EXHAUSTED_TTL_DEFAULT_SECONDS
    if failure_reason == FAILURE_REASON_BILLING_UNVERIFIED and error_code != 402:
        return min(base, EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS)
    is_billing = error_code == 402 or failure_reason == FAILURE_REASON_BILLING
    if sole_credential and not is_billing:
        return min(base, EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS)
    return base


def _normalize_error_context(error_context: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(error_context, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key in ("reason", "message"):
        value = error_context.get(key)
        if isinstance(value, str) and value.strip():
            normalized[key] = value.strip()
    reset_at = (
        error_context.get("reset_at")
        or error_context.get("resets_at")
        or error_context.get("retry_until")
    )
    parsed_reset_at = _parse_absolute_timestamp(reset_at)
    message = error_context.get("message")
    if parsed_reset_at is None and isinstance(message, str):
        retry_delay_seconds = reset_delay_from_message(message)
        if retry_delay_seconds is not None:
            parsed_reset_at = time.time() + retry_delay_seconds
    if parsed_reset_at is not None:
        normalized["reset_at"] = parsed_reset_at
    return normalized


def _exhausted_until(entry: PooledCredential, *, sole_credential: bool = False) -> Optional[float]:
    if entry.last_status != STATUS_EXHAUSTED:
        return None
    reset_at = _parse_absolute_timestamp(entry.last_error_reset_at)
    if reset_at is not None:
        return reset_at
    if entry.last_status_at:
        return entry.last_status_at + _exhausted_ttl(
            entry.last_error_code,
            sole_credential=sole_credential,
            failure_reason=entry.failure_reason,
        )
    return None


# --- Custom (OpenAI-compatible) endpoint pool keys ---


def _normalize_custom_pool_name(name: str) -> str:
    """Normalize a custom provider name for use as a pool key suffix."""
    return name.strip().lower().replace(" ", "-")


def _norm_url(url: Any) -> str:
    return str(url or "").strip().rstrip("/")


def _iter_custom_providers(config: Optional[dict] = None):
    """Yield ``(normalized_name, entry)`` from the merged custom-provider config view."""
    if config is None:
        config = _load_config_safe()
    if config is None:
        return
    try:
        from noesia_cli.config import get_compatible_custom_providers

        custom_providers = get_compatible_custom_providers(config)
    except Exception:
        return
    for entry in custom_providers or ():
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            yield _normalize_custom_pool_name(entry["name"]), entry


def _custom_entry_name_aliases(norm_name: str, entry: dict[str, Any]) -> set:
    aliases = {norm_name}
    provider_key = _normalize_custom_pool_name(str(entry.get("provider_key") or ""))
    if provider_key:
        aliases.add(provider_key)
    return aliases


def _requested_custom_name_aliases(provider_name: str) -> set:
    normalized = _normalize_custom_pool_name(provider_name)
    aliases = {normalized} if normalized else set()
    if normalized.startswith(CUSTOM_POOL_PREFIX):
        suffix = _normalize_custom_pool_name(normalized[len(CUSTOM_POOL_PREFIX):])
        if suffix:
            aliases.add(suffix)
    return aliases


def _pool_keys_for_custom_entry(norm_name: str, entry: dict[str, Any]) -> list[str]:
    """Durable ``providers.<key>`` slug first, then legacy ``custom:<name>``."""
    keys: list[str] = []
    provider_key = _normalize_custom_pool_name(str(entry.get("provider_key") or ""))
    for key in (provider_key, f"{CUSTOM_POOL_PREFIX}{norm_name}" if norm_name else ""):
        normalized = key.strip().lower()
        if normalized and normalized not in keys:
            keys.append(normalized)
    return keys


def custom_provider_pool_key_candidates(
    base_url: Optional[str], provider_name: Optional[str] = None, config: Optional[dict] = None,
) -> list[str]:
    """Return pool keys to try for a custom endpoint: the durable ``providers.<key>`` slug
    (``noesia auth add <key>``), then legacy ``custom:<display-name>``, so a populated pool is not
    skipped for the ``no-key-required`` placeholder. ``config`` is a frozen route's config: ITS
    entry owns the endpoint, so a live edit of that entry's URL neither drops nor redirects the pool.
    """
    # A named pool holds credentials for its configured endpoint only: a launch-overridden base_url
    # must not inherit them (R2-M1), nor borrow a sibling's by URL.
    from agent.custom_entry_pool import custom_entry_for_endpoint
    match = custom_entry_for_endpoint(base_url, provider_name, config)
    return _pool_keys_for_custom_entry(*match) if match else []


def get_custom_provider_pool_key(base_url: Optional[str], provider_name: Optional[str] = None) -> Optional[str]:
    """Preferred pool key for a custom provider: durable slug, else ``custom:<name>``.

    When provider_name is given, match by name first so two custom providers
    sharing a base_url keep separate keys.
    """
    candidates = custom_provider_pool_key_candidates(base_url, provider_name)
    return candidates[0] if candidates else None


def list_custom_pool_providers() -> list[str]:
    """Return all 'custom:*' pool keys that have entries in auth.json."""
    pool_data = read_credential_pool(None)
    return sorted(
        key for key in pool_data
        if key.startswith(CUSTOM_POOL_PREFIX)
        and isinstance(pool_data.get(key), list)
        and pool_data[key]
    )


def _get_custom_provider_config(pool_key: str) -> Optional[dict[str, Any]]:
    """Return the custom_providers config entry matching a pool key like 'custom:together.ai'."""
    if not pool_key.startswith(CUSTOM_POOL_PREFIX):
        return None
    suffix = pool_key[len(CUSTOM_POOL_PREFIX):]
    return next((entry for norm_name, entry in _iter_custom_providers() if norm_name == suffix), None)


def get_pool_strategy(provider: str) -> str:
    """Return the configured selection strategy for a provider."""
    config = _load_config_safe()
    strategies = config.get("credential_pool_strategies") if config else None
    if not isinstance(strategies, dict):
        return STRATEGY_FILL_FIRST
    strategy = str(strategies.get(provider, "") or "").strip().lower()
    return strategy if strategy in SUPPORTED_POOL_STRATEGIES else STRATEGY_FILL_FIRST


def _keyed_custom_pool_matches(
    pool_provider: str,
    provider_norm: str,
    base_url: Optional[str],
) -> bool:
    """Match a durable ``providers.<key>`` pool against runtime identities."""
    runtime_url = _norm_url(base_url)
    if not runtime_url:
        return False
    try:
        for normalized_name, entry in _iter_custom_providers():
            provider_key = _normalize_custom_pool_name(str(entry.get("provider_key") or ""))
            if provider_key != pool_provider:
                continue
            aliases = _custom_entry_name_aliases(normalized_name, entry)
            aliases.add(f"{CUSTOM_POOL_PREFIX}{normalized_name}")
            if provider_key:
                aliases.add(f"{CUSTOM_POOL_PREFIX}{provider_key}")
            configured_url = _norm_url(entry.get("base_url"))
            if provider_norm == "custom":
                return runtime_url == configured_url
            runtime_aliases = _requested_custom_name_aliases(provider_norm)
            return bool(runtime_aliases & aliases) and runtime_url == configured_url
    except Exception:
        return False
    return False


def _legacy_custom_pool_matches(
    pool_provider: str,
    provider_norm: str,
    runtime_url: str,
) -> bool:
    """Match a legacy ``custom:<name>`` pool against a named runtime identity."""
    try:
        for normalized_name, entry in _iter_custom_providers():
            if f"{CUSTOM_POOL_PREFIX}{normalized_name}" != pool_provider:
                continue
            aliases = {normalized_name}
            for value in (entry.get("name"), entry.get("provider_key")):
                alias = _normalize_custom_pool_name(str(value or ""))
                if alias:
                    aliases.add(alias)
                    if alias.startswith(CUSTOM_POOL_PREFIX):
                        aliases.add(alias[len(CUSTOM_POOL_PREFIX):])
            configured_url = _norm_url(entry.get("base_url"))
            runtime_aliases = {_normalize_custom_pool_name(provider_norm)}
            if provider_norm.startswith(CUSTOM_POOL_PREFIX):
                runtime_aliases.add(_normalize_custom_pool_name(provider_norm[len(CUSTOM_POOL_PREFIX):]))
            return bool(runtime_aliases & aliases) and runtime_url == configured_url
    except Exception:
        return False
    return False


def credential_pool_entry_serves_endpoint(entry: Any, base_url: Any) -> bool:
    """Whether a pooled credential may be bound to a session running at ``base_url``. ``_swap_credential``
    adopts the entry's base_url too, so a same-provider entry for another endpoint (public OpenAI vs. an
    Azure resource) would send the session's requests — and the entry's key — to the wrong host (#68237).
    Entries or sessions without endpoint metadata (legacy adapters, test doubles) cannot rebind and are accepted."""
    if not isinstance(base_url, str) or not base_url:
        return True
    entry_url = getattr(entry, "runtime_base_url", None) or getattr(entry, "base_url", None)
    if not isinstance(entry_url, str) or not entry_url:
        return True
    from noesia_cli.route_identity import normalize_route_base_url
    return normalize_route_base_url(entry_url) == normalize_route_base_url(base_url)


def credential_pool_matches_provider(
    pool_or_provider: Any,
    provider: Optional[str],
    *,
    base_url: Optional[str] = None,
) -> bool:
    """Return whether a pool belongs to the requested runtime provider.

    Named custom endpoints may use three identities: the live agent can retain
    the configured name/provider key, newer runtime paths normalize it to
    ``custom``, and the pool may be keyed as the durable ``providers.<key>``
    slug or as legacy ``custom:<name>``. Accept those aliases only when the
    runtime endpoint belongs to the same configured custom provider. Empty
    identities fail closed. Legacy pool adapters without a ``provider``
    attribute remain compatible; production pools are scoped.
    """
    raw_pool_provider = getattr(pool_or_provider, "provider", None)
    if raw_pool_provider is None:
        if not isinstance(pool_or_provider, str):
            # Lightweight/unscoped pool adapters (old plugins, tests) may
            # expose only select()/has_credentials().
            return True
        raw_pool_provider = pool_or_provider
    pool_provider = str(raw_pool_provider or "").strip().lower()
    provider_norm = str(provider or "").strip().lower()
    if not pool_provider or not provider_norm:
        return False
    if not pool_provider.startswith(CUSTOM_POOL_PREFIX):
        if pool_provider == provider_norm:
            return True
        return _keyed_custom_pool_matches(pool_provider, provider_norm, base_url)
    if provider_norm == "custom":
        try:
            matched_pool = get_custom_provider_pool_key(base_url or "")
            if str(matched_pool or "").strip().lower() == pool_provider:
                return True
            candidates = custom_provider_pool_key_candidates(base_url or "")
        except Exception:
            return False
        return pool_provider in {str(key).strip().lower() for key in candidates}

    runtime_url = _norm_url(base_url)
    if not runtime_url:
        return False
    return _legacy_custom_pool_matches(pool_provider, provider_norm, runtime_url)


def resolve_runtime_pool_key(provider: Optional[str], base_url: Optional[str]) -> str:
    """Resolve the credential-pool key for a runtime provider identity.

    Named custom runtimes retain their configured alias while their pool may
    be stored under the durable ``providers.<key>`` slug or legacy
    ``custom:<name>``. Return that scoped key only when the canonical
    provider/endpoint boundary accepts it; otherwise preserve the normalized
    runtime identity so callers fail closed.
    """
    provider_norm = str(provider or "").strip().lower()
    if not provider_norm:
        return ""

    def _accepts(candidate: str) -> bool:
        return credential_pool_matches_provider(candidate, provider_norm, base_url=base_url)

    try:
        if provider_norm == "custom":
            candidate = get_custom_provider_pool_key(base_url)
            if candidate and _accepts(candidate):
                return str(candidate).strip().lower()
        else:
            # Named/exact custom runtimes are keyed by identity: search the
            # configured candidates by identity before endpoint so a sibling
            # sharing the URL cannot lend its pool.
            for normalized_name, entry in _iter_custom_providers():
                for candidate in _pool_keys_for_custom_entry(normalized_name, entry):
                    if _accepts(candidate):
                        return candidate
    except Exception:
        pass
    return provider_norm


DEFAULT_MAX_CONCURRENT_PER_CREDENTIAL = 1


def persist_pool_entries(
    provider: str,
    payloads: list[dict[str, Any]],
    *,
    removed_ids: Optional[Iterable[str]] = None,
    status_cleared_ids: Optional[Iterable[str]] = None,
) -> Optional[list[dict[str, Any]]]:
    """Persist a provider's pool rows to the auth store."""
    return write_credential_pool(
        provider, payloads, removed_ids=removed_ids, status_cleared_ids=status_cleared_ids,
    )

class CredentialPool(CredentialPoolAdminMixin, CredentialPoolModelCooldownMixin):
    def __init__(self, provider: str, entries: list[PooledCredential]):
        self.provider = provider
        self._entries = sorted(entries, key=lambda entry: entry.priority)
        self._current_id: Optional[str] = None
        self._strategy = get_pool_strategy(provider)
        # RLock: _replace_entry/_persist self-acquire it; in-lock callers
        # re-acquire reentrantly.
        self._lock = threading.RLock()
        self._active_leases: dict[str, int] = {}
        self._max_concurrent = DEFAULT_MAX_CONCURRENT_PER_CREDENTIAL
        # Monotonic timestamp of the last "no available entries" log (see
        # NO_AVAILABLE_ENTRIES_LOG_THROTTLE_SECONDS). Re-armed to None on every
        # successful selection so a recover->re-exhaust transition logs promptly.
        self._last_no_entries_log_at: Optional[float] = None
        # #70401: consecutive mark_exhausted_and_rotate() calls whose supplied
        # credential identity matched no pool entry. These mark nothing
        # exhausted, so without a cap the pool never converges to "no available
        # entries" and the caller's 401 retry loop runs unbounded. Reset when a
        # real entry is identified or an escape path returns None.
        self._unmatched_rotation_streak: int = 0

    # ---- read accessors ---------------------------------------------------

    def has_credentials(self) -> bool:
        with self._lock:
            return bool(self._entries)

    def has_available(self, *, model: Optional[str] = None) -> bool:
        """True if at least one entry is not currently in exhaustion cooldown.

        ``_available_entries`` is not read-only (it prunes aged-out DEAD
        manual entries and persists), so it must run under ``self._lock``
        like every other caller or a probe can race a concurrent rotation.
        """
        with self._lock:
            available = self._available_entries(model=model)
            return bool(available)

    def lift_reopened_cooldowns(self, *, model: Optional[str] = None) -> bool:
        """Clear cooldowns that have elapsed or (Codex) reopened early, then report availability.

        The lift ``select()`` performs, without leasing an entry. ``has_available`` and
        ``next_available_at`` never run the early-reopen probe.
        """
        with self._lock:
            available = self._available_entries(clear_expired=True, model=model)
            return bool(available)

    def next_available_at(self, *, model: Optional[str] = None) -> Optional[float]:
        """Earliest epoch time (seconds) any entry re-enters rotation.

        ``None`` when an entry is available now, or when no exhausted entry
        carries a usable recovery time (empty pool, or only ``STATUS_DEAD``
        entries). Callers must treat ``None`` as "no wait information".
        Runs under ``self._lock`` for the same reason as ``has_available``.
        """
        with self._lock:
            available = self._available_entries(model=model)
            if available:
                return None
            # Mirror _available_entries: a sole credential's transient throttle
            # cools down in seconds, and the fallback restore gate must not
            # wait an hour for a 60s cooldown.
            sole_credential = self._is_sole_credential()
            candidates = [
                until
                for until in (
                    _exhausted_until(entry, sole_credential=sole_credential)
                    for entry in self._entries
                    if entry.last_status == STATUS_EXHAUSTED
                )
                if until is not None
            ]
            candidates.extend(
                until
                for entry in self._entries
                if entry.last_status != STATUS_DEAD
                for until in (model_cooldown_until(entry, model),)
                if until is not None
            )
            return min(candidates) if candidates else None

    def entries(self) -> list[PooledCredential]:
        with self._lock:
            return list(self._entries)

    def _is_sole_credential(self) -> bool:
        """DEAD entries never re-enter rotation, so <=1 non-DEAD entry means nothing to rotate to."""
        return sum(1 for e in self._entries if e.last_status != STATUS_DEAD) <= 1

    def _find(self, predicate: Callable[[PooledCredential], bool]) -> Optional[PooledCredential]:
        return next((e for e in self._entries if predicate(e)), None)

    def _current_unlocked(self) -> Optional[PooledCredential]:
        if not self._current_id:
            return None
        return self._find(lambda e: e.id == self._current_id)

    def current(self) -> Optional[PooledCredential]:
        with self._lock:
            return self._current_unlocked()

    def entry_id_for_api_key(self, api_key_hint: Any = None) -> Optional[str]:
        """Stable id for the runtime credential in use.

        Prefer the current selection when it still supplies ``api_key_hint``;
        if the cursor was cleared, fall back to an unambiguous key match.
        """
        with self._lock:
            current = self._current_unlocked()
            if current is not None and (api_key_hint is None or current.runtime_api_key == api_key_hint):
                return current.id
            if api_key_hint is None:
                return None
            matches = [e for e in self._entries if e.runtime_api_key == api_key_hint]
            return matches[0].id if len(matches) == 1 else None

    # ---- mutation primitives (self-locking) --------------------------------

    def _replace_entry(self, old: PooledCredential, new: PooledCredential) -> None:
        """Swap an entry in-place by id, preserving sort order.

        Self-locking (RLock) so the deferred refresh path — which runs outside
        the pool lock — cannot tear ``self._entries`` against a concurrent
        select()/rotation.
        """
        with self._lock:
            for idx, entry in enumerate(self._entries):
                if entry.id == old.id:
                    self._entries[idx] = new
                    return

    def _persist(
        self,
        *,
        removed_ids: Optional[list[str]] = None,
        status_cleared_ids: Optional[list[str]] = None,
    ) -> None:
        # Self-locking: snapshotting self._entries must not race a rotation.
        with self._lock:
            payloads = [entry.to_dict() for entry in self._entries]
            persist_pool_entries(
                self.provider, payloads,
                removed_ids=removed_ids,
                status_cleared_ids=status_cleared_ids,
            )

    def _adopt(self, entry: PooledCredential, *, persist: bool = True, **updates: Any) -> PooledCredential:
        """``replace(entry, **updates)``, swap it into the pool, optionally persist."""
        updated = replace(entry, **updates)
        self._replace_entry(entry, updated)
        if persist:
            self._persist()
        return updated

    # ---- exhaustion --------------------------------------------------------

    def _mark_exhausted(
        self,
        entry: PooledCredential,
        status_code: Optional[int],
        error_context: Optional[dict[str, Any]] = None,
        *,
        persist: bool = True,
        failure_reason: Optional[str] = None,
    ) -> PooledCredential:
        normalized_error = _normalize_error_context(error_context)
        # A DEAD row stays DEAD: re-entering rotation every hour would fail
        # immediately until the user replaces the key (#32849).
        terminal = entry.last_status == STATUS_DEAD
        # Carry the classifier's verdict so the cooldown is sized by what
        # actually failed (a billing 403 must not get the sole-credential
        # transient cooldown); absent a classification, clear a stale one.
        updated_extra = dict(entry.extra)
        if failure_reason:
            updated_extra["failure_reason"] = failure_reason
        else:
            updated_extra.pop("failure_reason", None)
        return self._adopt(
            entry,
            persist=persist,
            last_status=STATUS_DEAD if terminal else STATUS_EXHAUSTED,
            last_status_at=time.time(),
            last_error_code=status_code,
            last_error_reason=normalized_error.get("reason"),
            last_error_message=normalized_error.get("message"),
            last_error_reset_at=normalized_error.get("reset_at"),
            extra=updated_extra,
        )

    # ---- selection ---------------------------------------------------------

    def select(self, *, model: Optional[str] = None) -> Optional[PooledCredential]:
        with self._lock:
            entry = self._select_unlocked(model=model)
        if entry is not None:
            self._unmatched_rotation_streak = 0
        return entry

    def _reset_cleared_after(self, entry: PooledCredential) -> Optional[float]:
        """Epoch of a ``noesia auth reset`` persisted by another process AFTER *entry*'s status, else None."""
        try:
            row = next((p for p in read_credential_pool(self.provider)
                        if isinstance(p, dict) and p.get("id") == entry.id), None)
            cleared = _parse_absolute_timestamp((row or {}).get("status_cleared_at"))
        except Exception as exc:
            logger.debug("Pool entry %s: could not read reset marker: %s", entry.id, exc)
            return None
        return cleared if cleared and cleared > (entry.last_status_at or 0.0) else None

    def _resync_stale_entry(self, entry: PooledCredential) -> PooledCredential:
        """Honour a ``noesia auth reset`` written by another process while this pool is live.

        The in-memory cooldown would otherwise outlive it (#89415).
        """
        if entry.last_status not in {STATUS_EXHAUSTED, STATUS_DEAD}:
            return entry
        cleared_at = self._reset_cleared_after(entry)
        if cleared_at is not None:
            return self._adopt(entry, persist=False, **_MARK_OK, status_cleared_at=cleared_at)
        return entry

    def _available_entries(self, *, clear_expired: bool = False, model: Optional[str] = None) -> list[PooledCredential]:
        """Return entries not in cooldown.

        *clear_expired* resets elapsed cooldowns to STATUS_OK and persists.
        """
        now = time.time()
        cleared_any = False
        entries_to_prune: list[str] = []
        available: list[PooledCredential] = []
        sole_credential = self._is_sole_credential()
        for entry in self._entries:
            # Borrowed credentials persist as metadata-only references and are
            # hydrated from their live source on load; never lease an
            # unhydrated duplicate as an empty key.
            if not entry.runtime_api_key:
                continue
            synced = self._resync_stale_entry(entry)
            if synced is not entry:
                entry = synced
                cleared_any = True
            if entry.last_status == STATUS_DEAD:
                # Manual DEAD credentials are pruned after a 24h quiet window;
                # DEAD never re-enters via TTL — only a replacement key clears it.
                if _is_manual_source(entry.source):
                    dead_at = entry.last_status_at or 0
                    if dead_at and now - dead_at > DEAD_MANUAL_PRUNE_TTL_SECONDS:
                        logger.warning(
                            "credential pool: pruning DEAD manual entry %s "
                            "(reason=%s, age=%.1fh) — re-add via `noesia auth add %s`",
                            entry.label or entry.id[:8],
                            entry.last_error_reason or "unknown",
                            (now - dead_at) / 3600.0,
                            self.provider,
                        )
                        entries_to_prune.append(entry.id)  # can't mutate while iterating
                        cleared_any = True
                continue
            if model_cooldown_until(entry, model) is not None:
                continue
            if entry.last_status == STATUS_EXHAUSTED:
                exhausted_until = _exhausted_until(entry, sole_credential=sole_credential)
                if exhausted_until is not None and now < exhausted_until:
                    continue
                if clear_expired:
                    entry = self._find(lambda e, i=entry.id: e.id == i) or entry
                    entry = self._adopt(entry, persist=False, **_MARK_OK)
                    cleared_any = True
            available.append(entry)
        if entries_to_prune:
            pruned_ids = set(entries_to_prune)
            self._entries = [e for e in self._entries if e.id not in pruned_ids]
        if cleared_any:
            self._persist(removed_ids=entries_to_prune)
        return available

    def _log_no_available_entries(self) -> None:
        """Emit the empty-pool INFO line at most once per throttle window."""
        now = time.monotonic()
        last = self._last_no_entries_log_at
        if last is not None and (now - last) < NO_AVAILABLE_ENTRIES_LOG_THROTTLE_SECONDS:
            return
        self._last_no_entries_log_at = now
        logger.info("credential pool: no available entries (all exhausted or empty)")

    def _select_unlocked(self, *, count: bool = True, model: Optional[str] = None) -> Optional[PooledCredential]:
        """Select the best available entry.

        ``count=False`` skips the ``request_count`` bump for selections that are
        not going to serve a request.
        """
        available = self._available_entries(clear_expired=True, model=model)
        if not available:
            self._current_id = None
            self._log_no_available_entries()
            return None

        # The pool recovered; re-arm the throttle so a later re-exhaustion
        # logs immediately.
        self._last_no_entries_log_at = None

        if self._strategy == STRATEGY_RANDOM:
            entry = random.choice(available)
        elif self._strategy == STRATEGY_LEAST_USED and len(available) > 1:
            entry = min(available, key=lambda e: e.request_count)
        else:
            entry = available[0]
        # Count the selection under every strategy. The counter is ``least_used``'s
        # baseline and reaches auth.json on the next persist (exhaustion, rotation,
        # refresh); it used to move only while ``least_used`` was active.
        if count:
            entry = self._adopt(entry, persist=False, request_count=entry.request_count + 1)
        if self._strategy == STRATEGY_ROUND_ROBIN and len(available) > 1:
            rotated = [candidate for candidate in self._entries if candidate.id != entry.id]
            rotated.append(replace(entry, priority=len(self._entries) - 1))
            self._entries = [replace(candidate, priority=idx) for idx, candidate in enumerate(rotated)]
            self._persist()
            entry = self._find(lambda candidate: candidate.id == entry.id) or entry
        self._current_id = entry.id
        return entry

    def peek(self) -> Optional[PooledCredential]:
        with self._lock:
            current = self._current_unlocked()
            if current is not None:
                return current
            available = self._available_entries()
            return available[0] if available else None

    def reclaim(self, credential_id: str, *, model: Optional[str] = None) -> Optional[PooledCredential]:
        """Entry *credential_id* once its cooldown has lifted (cleared the way ``select`` would),
        else ``None``. Never bumps ``request_count`` or round-robin order: a live session asking
        "may I go back?" every turn is not a request."""
        with self._lock:
            available = self._available_entries(clear_expired=True, model=model)
        return next((e for e in available if e.id == credential_id), None)

    # ---- rotation ----------------------------------------------------------

    def _identify_failed_entry(
        self, credential_id: Optional[str], api_key_hint: Optional[str],
    ) -> Optional[PooledCredential]:
        """Resolve the entry that issued a failed request from its supplied identity."""
        entry = None
        if credential_id:
            entry = self._find(lambda e: e.id == credential_id)
            # #79156: when both identities disagree, trust the key that made
            # the request. A stale ``_credential_pool_entry_id`` (per-turn env
            # refresh rewrote ``api_key`` without rebinding the id) would
            # otherwise quarantine a healthy fallback for days.
            if entry is not None and api_key_hint and entry.runtime_api_key != api_key_hint:
                hint_entry = self._find(lambda e: e.runtime_api_key == api_key_hint)
                if hint_entry is not None:
                    logger.info(
                        "credential pool: credential_id %s runtime key "
                        "does not match api_key_hint; attributing failure "
                        "to key-matched entry %s instead (#79156)",
                        (entry.label or entry.id[:8]),
                        (hint_entry.label or hint_entry.id[:8]),
                    )
                # Otherwise the id is stale and the request key is not in the
                # pool — drop the id so we do not mark the wrong entry.
                entry = hint_entry
        if entry is None and api_key_hint:
            # Prefer the entry whose key actually failed: on a pool freshly
            # loaded from disk current() is None and _select_unlocked() would
            # return the NEXT key — the wrong one.
            entry = self._find(lambda e: e.runtime_api_key == api_key_hint)
        return entry

    def _rotate_unmatched(self) -> Optional[PooledCredential]:
        """Rotate without marking anything when the failed identity matches no entry.

        Falling through to current()/_select_unlocked() would bench an
        innocent healthy key for the full TTL. But this must be BOUNDED
        (#70401): a rotation can leave the 401's key hint matching no
        ``runtime_api_key``, so every retry lands here, nothing is marked, and
        the caller retries the same dead token forever (~6/sec, starving the
        event loop). Cap consecutive no-mark rotations at one lap of the
        available entries, then surface the error; no cooldown is written.
        """
        self._unmatched_rotation_streak += 1
        available_count = len(self._available_entries())
        if self._unmatched_rotation_streak > max(available_count, 1):
            logger.warning(
                "credential pool: failed credential identity matched no "
                "%s entry for %d consecutive rotations (pool size %d) — "
                "surfacing the error instead of rotating again",
                self.provider, self._unmatched_rotation_streak, available_count,
            )
            self._unmatched_rotation_streak = 0
            self._current_id = None
            return None
        logger.info(
            "credential pool: failed credential identity matched no %s "
            "entry; rotating without marking any credential exhausted",
            self.provider,
        )
        self._current_id = None
        next_entry = self._select_unlocked()
        if next_entry is not None and len(self._available_entries()) == 1:
            # A single-entry pool cannot rotate: returning its only entry would
            # report a recovery without changing the credential, and the
            # caller retries the same 401 indefinitely.
            self._unmatched_rotation_streak = 0
            self._current_id = None
            return None
        return next_entry

    def mark_exhausted_and_rotate(
        self,
        *,
        status_code: Optional[int],
        error_context: Optional[dict[str, Any]] = None,
        api_key_hint: Optional[str] = None,
        credential_id: Optional[str] = None,
        failure_reason: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[PooledCredential]:
        with self._lock:
            identity_supplied = bool(credential_id or api_key_hint)
            entry = self._identify_failed_entry(credential_id, api_key_hint)
            if entry is None and identity_supplied:
                return self._rotate_unmatched()
            # A real entry was identified — any prior unmatched streak is stale.
            self._unmatched_rotation_streak = 0
            if entry is None:
                entry = self._current_unlocked() or self._select_unlocked()
            if entry is None:
                return None
            _label = entry.label or entry.id[:8]
            if self._is_model_scoped_failure(status_code, model, failure_reason):
                # A generic Anthropic 429 (per-model rate limit) or a Codex account model
                # entitlement rejection: bench this model only, the credential stays
                # available for its siblings.
                self._cool_down_model(entry, model, error_context, failure_reason=failure_reason)
                logger.info("credential pool: %s unavailable for model %s; other models stay available", _label, model)
                self._current_id = None
                return self._select_unlocked(model=model)
            self._mark_exhausted(entry, status_code, error_context, failure_reason=failure_reason)
            # A 402/429/401 is a key-level failure, and the same key can back
            # several entries (an explicit entry plus a ``model_config`` row
            # auto-seeded from ``model.api_key``). Marking only the first
            # leaves siblings OK, ``_select_unlocked()`` keeps handing back
            # the depleted key, and rotation never converges (~2.5 min hang).
            # Mark every entry sharing the failed key.
            failed_runtime_key = entry.runtime_api_key
            if identity_supplied and failed_runtime_key:
                siblings = [
                    s for s in self._entries if s.id != entry.id and s.runtime_api_key == failed_runtime_key
                ]
                for sibling in siblings:
                    self._mark_exhausted(
                        sibling, status_code, error_context, persist=False, failure_reason=failure_reason,
                    )
                if siblings:
                    self._persist()
            # Re-read the updated entry to log the correct terminal state.
            updated_entry = self._find(lambda e: e.id == entry.id) or entry
            if updated_entry.last_status == STATUS_DEAD:
                logger.warning(
                    "credential pool: marking %s DEAD (status=%s, reason=%s) — "
                    "permanently failed, will NOT re-enter rotation until re-auth",
                    _label, status_code, updated_entry.last_error_reason or "unknown",
                )
            else:
                logger.info("credential pool: marking %s exhausted (status=%s), rotating", _label, status_code)
            self._current_id = None
            next_entry = self._select_unlocked()
            if next_entry is not None and next_entry.id == entry.id:
                # No-recovery guard (#97315): selection handed back the very entry that was
                # just marked (the auth-store sync adopted fresher tokens, or a quota probe
                # false-positive lifted the bench mid-selection). Returning it reports a
                # successful rotation without changing the credential, so the caller retries
                # the same 429 forever (~2 req/s for hours). Mirror the single-entry guard on
                # the unmatched-identity branch: surface the failure instead.
                logger.warning(
                    "credential pool: rotation returned the just-marked entry %s — "
                    "treating as no-recovery so the failure surfaces", _label,
                )
                self._current_id = None
                return None
            if next_entry:
                logger.info("credential pool: rotated to %s", next_entry.label or next_entry.id[:8])
            return next_entry

    # ---- leases ------------------------------------------------------------

    def acquire_lease(self, credential_id: Optional[str] = None) -> Optional[str]:
        """Acquire a soft lease on a credential.

        With *credential_id*, lease that entry directly. Otherwise prefer the
        least-leased available credential (priority as tie-breaker); when
        every credential is at the soft cap, still return the least-leased
        one instead of blocking.
        """
        with self._lock:
            if credential_id:
                self._active_leases[credential_id] = self._active_leases.get(credential_id, 0) + 1
                self._current_id = credential_id
                return credential_id

            available = self._available_entries(clear_expired=True)
            if not available:
                return None

            below_cap = [e for e in available if self._active_leases.get(e.id, 0) < self._max_concurrent]
            chosen = min(
                below_cap or available,
                key=lambda entry: (self._active_leases.get(entry.id, 0), entry.priority),
            )
            self._active_leases[chosen.id] = self._active_leases.get(chosen.id, 0) + 1
            self._current_id = chosen.id
            return chosen.id

    def release_lease(self, credential_id: str) -> None:
        with self._lock:
            count = self._active_leases.get(credential_id, 0)
            if count <= 1:
                self._active_leases.pop(credential_id, None)
            else:
                self._active_leases[credential_id] = count - 1



# --- Seeding --------------------------------------------------------------


def _upsert_entry(entries: list[PooledCredential], provider: str, source: str, payload: dict[str, Any]) -> bool:
    matching_indices = [idx for idx, entry in enumerate(entries) if entry.source == source]
    existing_idx = matching_indices[0] if matching_indices else None
    duplicate_indices = set(matching_indices[1:])
    if duplicate_indices:
        entries[:] = [entry for idx, entry in enumerate(entries) if idx not in duplicate_indices]

    if existing_idx is None:
        payload.setdefault("id", uuid.uuid4().hex[:6])
        payload.setdefault("priority", _next_priority(entries))
        payload.setdefault("label", payload.get("label") or source)
        entries.append(PooledCredential.from_dict(provider, payload))
        return True

    existing = entries[existing_idx]
    field_updates: dict[str, Any] = {}
    extra_updates: dict[str, Any] = {}
    _field_names = {f.name for f in fields(existing)}
    incoming_token = payload.get("access_token")
    token_changed = incoming_token is not None and incoming_token != existing.access_token
    if token_changed and not existing.access_token:
        # Borrowed sources (claude_code, env-backed rows) are written to
        # auth.json without their secret, so a reloaded entry carries only a
        # ``secret_fingerprint``. Comparing against the empty string reported
        # a rotation on EVERY load and cleared the DEAD/exhausted state the
        # previous process had just persisted. Compare fingerprints instead.
        known_fingerprint = existing.extra.get("secret_fingerprint")
        if isinstance(known_fingerprint, str) and known_fingerprint:
            token_changed = fingerprint_secret_value(incoming_token) != known_fingerprint
    for key, value in payload.items():
        if key in {"id", "priority"} or value is None or (key == "label" and existing.label):
            continue
        if key in _field_names:
            if getattr(existing, key) != value:
                field_updates[key] = value
        elif key in _EXTRA_KEYS and existing.extra.get(key) != value:
            extra_updates[key] = value
    # A rotated token makes the old exhaustion/error state stale.
    if token_changed and existing.last_status is not None:
        field_updates.update(_CLEAR_STATUS)
    if field_updates or extra_updates:
        if extra_updates:
            field_updates["extra"] = {**existing.extra, **extra_updates}
        updated = replace(existing, **field_updates)
        entries[existing_idx] = updated
        # Runtime-only borrowed secret updates refresh the in-memory entry
        # without forcing auth.json churn when the disk-safe payload is
        # unchanged (e.g. env keys with the same fingerprint).
        return bool(duplicate_indices) or existing.to_dict() != updated.to_dict()
    return bool(duplicate_indices)


_ANTHROPIC_SOURCE_RANK = {
    "env:ANTHROPIC_TOKEN": 0,
    "env:ANTHROPIC_API_KEY": 1,
}


def _normalize_pool_priorities(provider: str, entries: list[PooledCredential]) -> bool:
    if provider != "anthropic":
        return False
    manual_entries = sorted(
        (entry for entry in entries if _is_manual_source(entry.source)),
        key=lambda entry: entry.priority,
    )
    seeded_entries = sorted(
        (entry for entry in entries if not _is_manual_source(entry.source)),
        key=lambda entry: (
            _ANTHROPIC_SOURCE_RANK.get(entry.source, len(_ANTHROPIC_SOURCE_RANK)),
            entry.priority,
            entry.label,
        ),
    )
    id_to_idx = {entry.id: idx for idx, entry in enumerate(entries)}
    changed = False
    for new_priority, entry in enumerate([*manual_entries, *seeded_entries]):
        if entry.priority != new_priority:
            entries[id_to_idx[entry.id]] = replace(entry, priority=new_priority)
            changed = True
    return changed


class _Seeder:
    """Accumulates ``_upsert_entry`` results for one ``load_pool`` seeding pass."""

    def __init__(self, provider: str, entries: list[PooledCredential]):
        self.provider = provider
        self.entries = entries
        self.changed = False
        self.active_sources: set[str] = set()
        self.is_suppressed = _is_source_suppressed_fn()

    def upsert(self, source: str, payload: dict[str, Any]) -> bool:
        """Upsert unless suppressed (``noesia auth remove`` must stay stable across loads)."""
        if self.is_suppressed(self.provider, source):
            return False
        self.active_sources.add(source)
        ingested = _upsert_entry(self.entries, self.provider, source, {"source": source, **payload})
        self.changed |= ingested
        return ingested

    @property
    def result(self) -> tuple[bool, set[str]]:
        return self.changed, self.active_sources


# Warn once per token per process when Copilot exchange degrades to raw token (#114740).
_COPILOT_RAW_DEGRADATION_WARNED: set[str] = set()


def _warn_copilot_raw_degradation_once(token: str) -> None:
    """WARN once per token per process when Copilot exchange degrades to raw token (#114740)."""
    fingerprint = fingerprint_secret_value(token) or "unknown"
    if fingerprint in _COPILOT_RAW_DEGRADATION_WARNED:
        return
    _COPILOT_RAW_DEGRADATION_WARNED.add(fingerprint)
    logger.warning(
        "Copilot token exchange degraded to RAW token (exchange "
        "unavailable); enterprise-only models may 400 with "
        "model_not_available_for_integrator until exchange recovers."
    )


def _seed_copilot_singleton(seed: _Seeder) -> None:
    # Copilot tokens are resolved dynamically via `gh auth token` or env vars
    # (COPILOT_GITHUB_TOKEN / GH_TOKEN); they don't live in the auth store.
    try:
        from noesia_cli.copilot_auth import (
            COPILOT_ENV_VARS,
            resolve_copilot_token,
            get_copilot_api_token,
        )
        # All-sources gate BEFORE any work: resolve_copilot_token() shells out
        # and the exchange retries 3x with backoff (~35s worst case); a user
        # who suppressed every copilot source must not pay that on every pool
        # load. The source space here matches credential_sources._remove_copilot_gh.
        copilot_sources = ["gh_cli"] + [f"env:{v}" for v in COPILOT_ENV_VARS]
        if all(seed.is_suppressed(seed.provider, s) for s in copilot_sources):
            return
        token, source = resolve_copilot_token()
        if not token:
            return
        # Exact match: a substring test would classify GH_TOKEN/GITHUB_TOKEN
        # as gh_cli and bypass a user's per-env-var suppression.
        source_name = "gh_cli" if source == "gh auth token" else f"env:{source}"
        # Per-source gate BEFORE the (~35s worst case) network exchange.
        if seed.is_suppressed(seed.provider, source_name):
            return
        from noesia_cli.auth import is_provider_explicitly_configured
        if not is_provider_explicitly_configured(seed.provider):
            # Copilot is only discovered here (ambient gh CLI login), not selected anywhere: no
            # model will be routed to it, so the network exchange — and its degradation warning on
            # every pool load — buys nothing (#114740). Seed the raw token; the load that follows
            # the user selecting copilot re-seeds and exchanges.
            api_token, enterprise_base_url = token, None
        else:
            api_token, enterprise_base_url = get_copilot_api_token(token)
            # get_copilot_api_token falls back to the RAW token when the exchange
            # fails; the Copilot API then routes it to the fallback
            # "copilot-language-server" integrator whose allowlist omits
            # enterprise-only models -> HTTP 400 on every turn. Surface it once.
            if api_token == token and not enterprise_base_url:
                _warn_copilot_raw_degradation_once(token)
        pconfig = PROVIDER_REGISTRY.get(seed.provider)
        seed.upsert(source_name, {
            "auth_type": AUTH_TYPE_API_KEY,
            "access_token": api_token,
            "base_url": enterprise_base_url or (pconfig.inference_base_url if pconfig else ""),
            "label": source,
        })
    except Exception as exc:
        logger.debug("Copilot token seed failed: %s", exc)


def get_env_prefer_dotenv(key: str) -> str:
    """Resolve a credential env var, preferring ~/.noesia/.env over os.environ.

    The user's config file is authoritative; stale env vars from parent
    processes (Codex CLI, test scripts) must not override deliberate .env
    changes. load_env() memoizes on mtime, so per-call reads cost a stat().
    An unresolved ``op://`` reference in .env yields to the already-resolved
    value from the active secret scope (set by apply_onepassword_secrets());
    otherwise every provider auth attempt would receive a URL instead of a key.
    """
    env_file = load_env()
    raw = env_file.get(key, "").strip()
    scoped_value = (_get_secret(key, "") or "").strip()
    if raw.startswith("op://") and scoped_value:
        return scoped_value
    return raw or scoped_value


# Providers already warned about env-key -> pool ingestion, once per process
# (#81952 expected-behavior #3).
_ENV_INGESTION_WARNED: set[str] = set()


def _warn_env_ingestion_once(provider: str, env_var: str) -> None:
    """WARN once per process per provider when an env credential is ingested into a paid pool.

    Auto-ingesting OPENROUTER_API_KEY is what ARMS silent OpenRouter spend —
    every downstream auto-detect keys off the pool having credentials.
    Ingestion stays allowed (an exported key is arguable intent) but must
    never be silent.
    """
    if provider in _ENV_INGESTION_WARNED:
        return
    _ENV_INGESTION_WARNED.add(provider)
    logger.warning(
        "Ingested %s from environment into the %s credential pool — this "
        "enables %s spend. Remove the key or run "
        "noesia auth remove %s <n> to suppress.",
        env_var,
        provider,
        "OpenRouter" if provider == "openrouter" else provider,
        provider,
    )


def _env_payload(*, env_var: str, token: str, base_url: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "auth_type": AUTH_TYPE_API_KEY,
        "access_token": token,
        "base_url": base_url,
        "label": env_var,
    }
    try:
        from noesia_cli.env_loader import get_secret_source
        source_label = get_secret_source(env_var)
    except Exception:
        source_label = None
    secret_source = str(source_label).strip() if source_label else None
    if secret_source:
        payload["secret_source"] = secret_source
    return payload


# Region-specific endpoints inferred from the key itself.
_ENV_BASE_URL_RESOLVERS = {
    "kimi-coding": _resolve_kimi_base_url,
    "zai": _resolve_zai_base_url,
}


def _env_key_var_candidates(env_vars: list[str], entries: list[PooledCredential]) -> list[str]:
    """*env_vars*, their numbered siblings, and the ``env:VAR`` names already persisted.

    ``VAR_2``, ``VAR_3``, ... are tried for every declared VAR until the first
    one that does not resolve, so a `.env` or secret-manager project can back a
    whole rotation pool with no config: setting ``NVIDIA_API_KEY_2`` is the
    whole opt-in (#76593).

    Env-backed rows are written to auth.json without their secret and
    re-hydrated on every load; a row whose VAR the registry does not
    declare would otherwise stay empty forever and be silently dropped
    from rotation by ``_available_entries``.
    """
    names = list(env_vars)
    for base in env_vars:
        n = 2
        while get_env_prefer_dotenv(f"{base}_{n}"):
            names.append(f"{base}_{n}")
            n += 1
    for entry in entries:
        if entry.source.startswith("env:"):
            env_name = entry.source.split(":", 1)[1].strip()
            if env_name and env_name not in names:
                names.append(env_name)
    return names


def _seed_from_env(provider: str, entries: list[PooledCredential]) -> tuple[bool, set[str]]:
    seed = _Seeder(provider, entries)
    # Copilot's singleton branch exchanges the raw ghu_ GitHub token for the
    # api token via `get_copilot_api_token`; the generic loop would re-read
    # COPILOT_GITHUB_TOKEN and overwrite it with the RAW token, causing 400s
    # ("not available for integrator copilot-language-server").
    if provider == "copilot":
        return seed.result

    if provider == "openrouter":
        for env_var in _env_key_var_candidates(["OPENROUTER_API_KEY"], entries):
            token = get_env_prefer_dotenv(env_var)
            if token and seed.upsert(
                f"env:{env_var}",
                _env_payload(env_var=env_var, token=token, base_url=OPENROUTER_BASE_URL),
            ):
                _warn_env_ingestion_once(provider, env_var)
        return seed.result

    pconfig = PROVIDER_REGISTRY.get(provider)
    if not pconfig or pconfig.auth_type != AUTH_TYPE_API_KEY:
        return seed.result

    env_url = ""
    if pconfig.base_url_env_var:
        env_url = get_env_prefer_dotenv(pconfig.base_url_env_var).rstrip("/")

    env_vars = list(pconfig.api_key_env_vars)
    if provider == "anthropic":
        env_vars = ["ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY"]
    env_vars = _env_key_var_candidates(env_vars, entries)

    resolve_base_url = _ENV_BASE_URL_RESOLVERS.get(provider)
    for env_var in env_vars:
        token = get_env_prefer_dotenv(env_var)
        if not token:
            continue
        base_url = env_url or pconfig.inference_base_url
        if resolve_base_url is not None:
            base_url = resolve_base_url(token, pconfig.inference_base_url, env_url)
        seed.upsert(f"env:{env_var}", _env_payload(env_var=env_var, token=token, base_url=base_url))
    return seed.result


def _prune_stale_seeded_entries(
    entries: list[PooledCredential],
    active_sources: set[str],
    *,
    prune_env_sources: bool = True,
) -> bool:
    def _is_prunable(entry: PooledCredential) -> bool:
        # ``env:*`` entries are persisted references re-hydrated on every load.
        # A process that merely lacks the env var must NOT delete the on-disk
        # entry for every other process (#9331); prune only when explicitly
        # requested (an `noesia auth` command that confirmed the source is gone).
        if entry.source.startswith("env:"):
            return prune_env_sources
        return True

    retained = [
        entry
        for entry in entries
        if _is_manual_source(entry.source) or entry.source in active_sources or not _is_prunable(entry)
    ]
    if len(retained) == len(entries):
        return False
    entries[:] = retained
    return True


def _seed_custom_pool(pool_key: str, entries: list[PooledCredential]) -> tuple[bool, set[str]]:
    """Seed a custom endpoint pool from custom_providers config and model config."""
    seed = _Seeder(pool_key, entries)

    cp_config = _get_custom_provider_config(pool_key)
    if cp_config:
        api_key = str(cp_config.get("api_key") or "").strip()
        name = str(cp_config.get("name") or "").strip()
        if api_key:
            seed.upsert(f"config:{name}", {
                "auth_type": AUTH_TYPE_API_KEY,
                "access_token": api_key,
                "base_url": _norm_url(cp_config.get("base_url")),
                "label": name or f"config:{name}",
            })

    # Seed from model.api_key when model.provider=='custom' and model.base_url matches
    try:
        config = _load_config_safe()
        model_cfg = config.get("model") if config else None
        if isinstance(model_cfg, dict):
            model_provider = str(model_cfg.get("provider") or "").strip().lower()
            model_base_url = _norm_url(model_cfg.get("base_url"))
            model_api_key = next(
                (v.strip() for k in ("api_key", "api") for v in (model_cfg.get(k),) if isinstance(v, str) and v.strip()),
                "",
            )
            if model_provider == "custom" and model_base_url and model_api_key:
                # The pool may be keyed under the durable ``providers.<key>``
                # slug or legacy ``custom:<name>``; accept any candidate, or
                # seeding is skipped when the pool holds the other identity.
                # Check if this model's base_url matches our custom provider. See #100413.
                matched_keys = {
                    str(key).strip().lower() for key in custom_provider_pool_key_candidates(model_base_url)
                }
                if pool_key in matched_keys:
                    seed.upsert("model_config", {
                        "auth_type": AUTH_TYPE_API_KEY,
                        "access_token": model_api_key,
                        "base_url": model_base_url,
                        "label": "model_config",
                    })
    except Exception:
        pass

    return seed.result


def load_pool(provider: str) -> CredentialPool:
    provider = (provider or "").strip().lower()
    raw_entries = read_credential_pool(provider)
    disk_ids = {e.get("id") for e in raw_entries if isinstance(e, dict) and e.get("id")}
    changed = any(
        isinstance(payload, dict) and sanitize_borrowed_credential_payload(payload) != payload
        for payload in raw_entries
    )
    entries = [PooledCredential.from_dict(provider, payload) for payload in raw_entries]

    if provider.startswith(CUSTOM_POOL_PREFIX):
        custom_changed, custom_sources = _seed_custom_pool(provider, entries)
        changed |= custom_changed
        changed |= _prune_stale_seeded_entries(entries, custom_sources)
    else:
        env_changed, env_sources = _seed_from_env(provider, entries)
        changed |= env_changed
        active_sources = set(env_sources)
        if provider == "copilot":
            copilot_seed = _Seeder(provider, entries)
            _seed_copilot_singleton(copilot_seed)
            changed |= copilot_seed.changed
            active_sources |= copilot_seed.active_sources
        # ``load_pool()`` is a non-destructive read for env-seeded entries
        # (#9331); other sources prune when their backing config is gone.
        changed |= _prune_stale_seeded_entries(entries, active_sources, prune_env_sources=False)
        changed |= _normalize_pool_priorities(provider, entries)

    pool = CredentialPool(provider, entries)
    if changed:
        pool._persist(removed_ids=sorted(disk_ids - {entry.id for entry in entries}))
    return pool
