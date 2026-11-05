"""Signed-in provider requests that core makes FOR plugins; the token never reaches plugin code.

A plugin that needs a third-party sign-in declares it under ``oauth:`` in its ``plugin.yaml`` and calls
:func:`credentialed_provider_request`. Noesia resolves the grant through its own locked-refresh path,
attaches ``Authorization`` itself, sends only to that provider's declared origins, never follows a
redirect, and hands back status, headers and body.

The caller is identified by file location: the nearest stack frame inside an installed plugin
directory. Like capabilities this is consent and visibility, not a sandbox. In-process plugin code
can still read any file the user can.
"""

from __future__ import annotations

import json as _json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from noesia_cli.urllib_security import url_origin


class ProviderNotSignedIn(RuntimeError):
    """The user has no usable sign-in for the provider in this Noesia profile."""


@dataclass(frozen=True)
class ProviderResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return _json.loads(self.body)


def _plugin_roots() -> set[Path]:
    """Every plugins directory an installed plugin can live under, as given and resolved."""
    from noesia_cli.plugins import get_bundled_plugins_dir
    from noesia_constants import get_default_noesia_root, get_noesia_home, get_process_noesia_home

    bases = (get_noesia_home() / "plugins", get_process_noesia_home() / "plugins",
             get_default_noesia_root() / "plugins", get_bundled_plugins_dir())
    return {r for base in bases for r in (base.absolute(), base.resolve())}


def _calling_plugin_dir() -> Optional[Path]:
    """Directory of the plugin whose code is nearest on the calling stack, or None."""
    roots = _plugin_roots()
    frame = sys._getframe(2)
    while frame is not None:
        raw = Path(frame.f_code.co_filename)
        for path in (raw.absolute(), raw.resolve()):
            root = next((r for r in roots if path.is_relative_to(r) and path != r), None)
            if root is None:
                continue
            for parent in path.parents:
                if parent == root:
                    break
                if (parent / "plugin.yaml").is_file() or (parent / "plugin.yml").is_file():
                    return parent
            return root / path.relative_to(root).parts[0]
        frame = frame.f_back
    return None


def _detached(exc: BaseException) -> BaseException:
    """*exc* without traceback or chain: those frames hold the token, refresh token or client secret."""
    exc.__cause__ = exc.__context__ = None
    return exc.with_traceback(None)


def credentialed_provider_request(
    provider: str, method: str, url: str, *, json: Any = None,
    headers: Optional[Mapping[str, str]] = None, timeout: float = 30.0,
) -> ProviderResponse:
    """Send ``method url`` with the user's *provider* sign-in attached by Noesia.

    *provider* is an ``id`` the calling plugin declares under ``oauth:``
    (:mod:`noesia_cli.plugin_oauth`). Refuses (``PermissionError``) a caller outside an installed
    plugin and any origin outside the provider's allowlist; raises :class:`ProviderNotSignedIn` when
    this profile has no sign-in. Redirects are returned as-is, never followed. The result carries no
    credential.
    """
    from noesia_cli.plugin_oauth import _plugin_oauth_spec

    plugin_dir = _calling_plugin_dir()
    if plugin_dir is None:
        raise PermissionError("credentialed_provider_request must be called from an installed plugin's code")
    spec = _plugin_oauth_spec(plugin_dir, provider)
    if spec is None:
        raise ValueError(f"No sign-in declared for provider {provider!r}; add it under `oauth:` in plugin.yaml")
    origins, resolve = spec
    if url_origin(url) not in origins:
        raise PermissionError(f"Refusing to send the {provider} sign-in to {url_origin(url)[1] or url!r}")
    import httpx

    # Every error the plugin receives past this point is rebuilt without traceback, chain or request: those
    # reach the frames and httpx objects that hold the token, refresh token or client secret.
    refusal: Optional[BaseException] = None
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        request = client.build_request(method, url, json=json, headers=dict(headers or {}))  # no credential yet
        try:
            auth_headers, credential_base = resolve(url)
        except httpx.HTTPError as exc:  # a resolver's own refresh POST failed: its request holds the refresh token
            refusal = type(exc)(str(exc))
        except (RuntimeError, OSError, ValueError, LookupError, TypeError) as exc:
            refusal = _detached(exc)
        else:
            if url_origin(credential_base) not in origins:
                refusal = PermissionError(
                    f"This profile's {provider} credential routes to a custom endpoint; refusing")
            else:
                for name in auth_headers:
                    request.headers.pop(name, None)
                request.headers.update(auth_headers)
                try:
                    response = client.send(request)
                except httpx.HTTPError as exc:
                    refusal = type(exc)(str(exc))
            auth_headers = request = None
    if refusal is not None:
        raise refusal
    return ProviderResponse(response.status_code, dict(response.headers), response.content)


__all__ = ["ProviderNotSignedIn", "ProviderResponse", "credentialed_provider_request"]
