from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from typing import TYPE_CHECKING, Any, Callable, Optional

import httpx

from noesia_cli.runtime_provider import resolve_runtime_provider
from noesia_time import safe_strftime

if TYPE_CHECKING:
    from typing import TypeGuard

logger = logging.getLogger(__name__)



def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class AccountUsageWindow:
    label: str
    used_percent: Optional[float] = None
    reset_at: Optional[datetime] = None
    detail: Optional[str] = None
    # ``account``: exhausting this window exhausts the whole login (Codex session/weekly).
    # ``model``: the window caps only one model family (Anthropic Opus/Sonnet weekly) and can
    # never imply the account itself is out of quota.
    scope: str = "account"


@dataclass(frozen=True)
class AccountUsageSnapshot:
    provider: str
    source: str
    fetched_at: datetime
    title: str = "Account limits"
    plan: Optional[str] = None
    windows: tuple[AccountUsageWindow, ...] = ()
    details: tuple[str, ...] = ()
    unavailable_reason: Optional[str] = None
    # Stable account identity of the credential the snapshot was fetched with (e.g. a decoded
    # Codex JWT principal), when the provider supports one. Never a secret; ``None`` = the
    # fetcher cannot tell accounts apart, so the snapshot belongs to the provider's legacy slot.
    identity: Optional[str] = None
    # Exact decoded provider response body (no headers/credentials) for integrations that need
    # fields Noesia does not normalize yet. Only populated by providers that fetch a JSON body.
    raw: Optional[dict] = None

    @property
    def available(self) -> bool:
        return bool(self.windows or self.details) and not self.unavailable_reason


def _snapshot(provider: str, source: str, windows: list, details: list, **kw: Any) -> AccountUsageSnapshot:
    return AccountUsageSnapshot(provider=provider, source=source, fetched_at=_utc_now(), windows=tuple(windows), details=tuple(details), **kw)


def _title_case_slug(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip()
    return cleaned.replace("_", " ").replace("-", " ").title() if cleaned else None


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if not isinstance(value, str) or not (text := value.strip()):
        return None
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _format_reset(dt: Optional[datetime]) -> str:
    if not dt:
        return "unknown"
    stamp = safe_strftime(dt.astimezone(), "%Y-%m-%d %H:%M %Z")
    total_seconds = int((dt - _utc_now()).total_seconds())
    if total_seconds <= 0:
        return f"now ({stamp})"
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"in {days}d {hours}h ({stamp})"
    return f"in {hours}h {minutes}m ({stamp})" if hours else f"in {minutes}m ({stamp})"


def render_account_usage_lines(snapshot: Optional[AccountUsageSnapshot], *, markdown: bool = False) -> list[str]:
    if not snapshot:
        return []
    bold = "**" if markdown else ""
    plan = f" ({snapshot.plan})" if snapshot.plan else ""
    lines = [f"📈 {bold}{snapshot.title}{bold}", f"Provider: {snapshot.provider}{plan}"]
    for window in snapshot.windows:
        if window.used_percent is None:
            base = f"{window.label}: unavailable"
        else:
            used = float(window.used_percent)
            base = f"{window.label}: {max(0, round(100 - used))}% remaining ({max(0, round(used))}% used)"
        if window.reset_at:
            base += f" • resets {_format_reset(window.reset_at)}"
        elif window.detail:
            base += f" • {window.detail}"
        lines.append(base)
    lines.extend(snapshot.details)
    if snapshot.unavailable_reason:
        lines.append(f"Unavailable: {snapshot.unavailable_reason}")
    return lines




def _is_num(v: Any) -> TypeGuard[float]:
    return isinstance(v, (int, float))
















def _fetch_openrouter_account_usage(base_url: Optional[str], api_key: Optional[str]) -> Optional[AccountUsageSnapshot]:
    runtime = resolve_runtime_provider(requested="openrouter", explicit_base_url=base_url, explicit_api_key=api_key)
    token = str(runtime.get("api_key", "") or "").strip()
    if not token:
        return None
    normalized = str(runtime.get("base_url", "") or "").rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with httpx.Client(timeout=10.0) as client:
        def _data(path: str) -> dict:
            resp = client.get(f"{normalized}/{path}", headers=headers)
            resp.raise_for_status()
            return (resp.json() or {}).get("data") or {}
        credits = _data("credits")
        try:
            key_data = _data("key")
        except Exception:
            key_data = {}
    balance = float(credits.get("total_credits") or 0.0) - float(credits.get("total_usage") or 0.0)
    details = [f"Credits balance: ${max(0.0, balance):.2f}"]
    windows: list[AccountUsageWindow] = []
    limit, limit_remaining, usage = key_data.get("limit"), key_data.get("limit_remaining"), key_data.get("usage")
    limit_reset = str(key_data.get("limit_reset") or "").strip()
    if _is_num(limit) and float(limit) > 0 and _is_num(limit_remaining) and 0 <= float(limit_remaining) <= float(limit):
        limit_value, remaining_value = float(limit), float(limit_remaining)
        detail_parts = [f"${remaining_value:.2f} of ${limit_value:.2f} remaining", *([f"resets {limit_reset}"] if limit_reset else [])]
        windows.append(AccountUsageWindow(label="API key quota", used_percent=((limit_value - remaining_value) / limit_value) * 100,
                                          detail=" • ".join(detail_parts)))
    if _is_num(usage):
        usage_parts = [f"API key usage: ${float(usage):.2f} total"]
        for key, label in (("usage_daily", "today"), ("usage_weekly", "this week"), ("usage_monthly", "this month")):
            value = key_data.get(key)
            if _is_num(value) and float(value) > 0:
                usage_parts.append(f"${float(value):.2f} {label}")
        details.append(" • ".join(usage_parts))
    return _snapshot("openrouter", "credits_api", windows, details)


_USAGE_FETCHERS: dict[str, Callable[[Optional[str], Optional[str]], Optional[AccountUsageSnapshot]]] = {
    "openrouter": _fetch_openrouter_account_usage,
}


# Wall-clock bound on a plugin profile's ``fetch_account_usage`` hook. The built-in fetchers above carry
# their own httpx timeouts; a plugin hook is arbitrary code, and the gateway/TUI ``/usage`` paths await
# this function with no deadline of their own (only the CLI wraps it in a 10 s future), so the bound
# lives here where every surface shares it.
PLUGIN_USAGE_HOOK_DEADLINE_S = 10.0


def _call_plugin_usage_hook(profile, base_url: Optional[str], api_key: Optional[str]) -> Optional[AccountUsageSnapshot]:
    """Run the profile hook under the shared deadline; past it → None. Exceptions re-raise in the
    caller so ``fetch_account_usage`` fails open without a worker-thread traceback on ``/usage``."""
    from agent.deadline import run_bounded_sync
    from providers.base import ProviderProfile

    if type(profile).fetch_account_usage is ProviderProfile.fetch_account_usage:
        return None  # base no-op: no thread to spawn
    bounded = run_bounded_sync(
        lambda: profile.fetch_account_usage(base_url=base_url, api_key=api_key),
        PLUGIN_USAGE_HOOK_DEADLINE_S, label="plugin-account-usage")
    return None if bounded.timed_out else bounded.value


def fetch_account_usage(
    provider: Optional[str], *, base_url: Optional[str] = None, api_key: Optional[str] = None,
    read_only: bool = False, identity_id: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    from agent.account_usage_cache import remember_account_usage

    slug = str(provider or "").strip().lower()
    fetcher = _USAGE_FETCHERS.get(slug)
    import time as _time

    started = _time.monotonic()
    try:
        if fetcher:
            snapshot = fetcher(base_url, api_key)
        else:
            from providers import get_provider_profile

            profile = get_provider_profile(slug)
            snapshot = _call_plugin_usage_hook(profile, base_url, api_key) if profile else None
    except Exception:
        return None
    # Every fetch (``/usage``, the per-turn ``session.usage``) keeps the picker's cache current.
    # A per-credential fetch is remembered under its account slot only; the provider-wide fetch
    # keeps both the legacy gauge slot and (when the fetcher identifies the account) that
    # account's slot current.
    remember_account_usage(provider, snapshot, identity_id=identity_id, base_url=base_url,
                           started_monotonic=started)
    return snapshot
