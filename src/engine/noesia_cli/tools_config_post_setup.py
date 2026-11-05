"""Post-setup install hooks and installed-state predicates for `noesia tools` provider rows."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from typing import Set

from noesia_cli.cli_output import (
    print_error as _print_error, print_info as _print_info, print_success as _print_success,
    print_warning as _print_warning)
from noesia_cli.config import get_env_value
from noesia_cli.tools_config_cua import _cua_driver_install_ready, install_cua_driver
from tools.transcription_common import DEFAULT_LOCAL_MODEL, STT_MODEL_CATALOG

_LOCAL_STT_MODEL_SUMMARY = ", ".join(
    f"{model} (default)" if model == DEFAULT_LOCAL_MODEL else model for model in STT_MODEL_CATALOG["local"]
)


def _info_lines(*lines: str) -> None:
    """Print each line as a 4-space-indented info row."""
    for line in lines:
        _print_info(f"    {line}")


def _ensure_browser_use_cli(*, verbose_hints: bool = False) -> None:
    """Confirm the Browser Use CLI engine is runnable. It is browser-harness, a core dependency of
    Noesia's own venv, so there is nothing to download; a miss means the venv needs a re-sync.
    Primary driver engine for EVERY browser backend except Camofox (Firefox-based, no CDP surface)."""
    from tools.browser_use_cli import _find_cli

    if _find_cli() is not None:
        _print_success("    Browser Use CLI ready (browser-harness, bundled with Noesia)")
    else:
        _print_warning("    browser-harness is missing from Noesia's Python environment")
        _print_info("    Re-sync it with: noesia update")
    if verbose_hints:
        _info_lines("Local Chrome needs remote debugging: chrome://inspect/#remote-debugging",
                    "Cloud browsers: set BROWSER_USE_API_KEY")


def _post_setup_lightpanda() -> None:
    # Browser Use mode spawns ``lightpanda serve``; built-in tools go through agent-browser. No Chromium needed.
    _ensure_browser_use_cli()
    from tools.browser_lightpanda import LIGHTPANDA_INSTALL_HINT, find_lightpanda_binary

    lightpanda_bin = find_lightpanda_binary()
    if lightpanda_bin:
        _print_success(f"    Lightpanda found: {lightpanda_bin}")
    else:
        _print_warning("    lightpanda binary not found on PATH, ~/.lightpanda or ~/.local/bin")
        _print_info(f"    {LIGHTPANDA_INSTALL_HINT}")
        if os.name == "nt":
            _print_info("    Lightpanda has no native Windows build; run Noesia under WSL2.")


def _post_setup_agent_browser(post_setup_key: str) -> None:
    """PM owns the driver and Chromium; Termux and Docker own their native payloads."""
    # Every non-Camofox backend drives through the Browser Use CLI — install it here too.
    _ensure_browser_use_cli()
    try:
        from tools.browser_tool_install import (
            _browser_install_hint, _chromium_installed, _running_in_docker, _find_agent_browser)
        from noesia_constants import is_termux
    except Exception as exc:  # pragma: no cover — defensive
        _print_warning(f"    Could not check Chromium status: {exc}")
        return

    termux = is_termux()
    docker = _running_in_docker()
    if termux or docker:
        try:
            _find_agent_browser(validate=False)
        except FileNotFoundError:
            _print_warning(f"    agent-browser is missing. Install it explicitly: {_browser_install_hint()}")
            return
        if docker and post_setup_key == "agent_browser" and not _chromium_installed():
            _print_warning("    Chromium is missing but you're running in Docker.")
            _info_lines("Pull the latest image to get the bundled Chromium:",
                        "  docker pull ghcr.io/noesia/noesia-agent:latest")
        return

    try:
        import pm
        # Chromium is a declared dependency; do not acquire it a second time.
        pm.ensure("agent-browser", explicit=True)
    except Exception as exc:
        _print_warning(f"    agent-browser install failed: {exc}")
        _info_lines("Retry with: noesia tools post-setup " + post_setup_key)
        return
    _print_success("    Managed agent-browser and Chromium are ready")

    # OS libraries are host-owned. Never download another package manager to install them.
    if post_setup_key == "agent_browser" and sys.platform == "linux":
        _info_lines("Chromium also needs system libraries supplied by your distribution.")
        if shutil.which("apt-get") and _module_installed("playwright"):
            command = shlex.join([sys.executable, "-m", "playwright", "install-deps", "chromium"])
            _info_lines(f"Install missing system libraries with: {command}")
        else:
            _info_lines("System dependency installation guide:",
                        "  https://playwright.dev/python/docs/browsers#install-system-dependencies")


def _post_setup_camofox() -> None:
    from tools.browser_camofox import check_camofox_available

    _info_lines("Camofox is an externally managed server; Noesia does not install or start it.")
    if check_camofox_available():
        _print_success("    Configured Camofox server is reachable")
        return
    _print_warning("    Camofox server is not reachable. Start your server and check CAMOFOX_URL.")
    _info_lines("Server setup: https://github.com/jo-inc/camofox-browser",
                "Docker: docker run -p 9377:9377 -e CAMOFOX_PORT=9377 jo-inc/camofox-browser")


# The hook key is the UI provider identifier; extra names belong to pyproject.toml.
def _python_hook(module, extra, label, installing, on_install=(), always=()) -> dict:
    return {"module": module, "extra": extra, "label": label, "installing": installing,
            "on_install": on_install, "always": always}


_PYTHON_POST_SETUP_HOOKS: dict = {
    "faster_whisper": _python_hook(
        "faster_whisper", "stt-whisper", "faster-whisper", "Installing faster-whisper (model ~150MB downloads on first use)...",
        on_install=(f"Model sizes: {_LOCAL_STT_MODEL_SUMMARY}",
                    "Change via stt.local.model in config.yaml")),
    "kittentts": _python_hook(
        "kittentts", "kittentts", "kittentts", "Installing kittentts (~25-80MB model, CPU-only)...",
        on_install=("Voices: Jasper, Bella, Luna, Bruno, Rosie, Hugo, Kiki, Leo",
                    "Models: KittenML/kitten-tts-nano-0.8-int8 (25MB), micro (41MB), mini (80MB)")),
    "piper": _python_hook(
        "piper", "piper", "piper-tts", "Installing piper-tts (~14MB wheel, voices downloaded on first use)...",
        always=("Default voice: en_US-lessac-medium (downloaded on first TTS call)",
                "Full voice list: https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md",
                "Switch voices by setting tts.piper.voice in config.yaml")),
    "ddgs": _python_hook(
        "ddgs", "ddgs", "ddgs", "Installing ddgs (DuckDuckGo search package)...",
        always=("No API key required. DuckDuckGo enforces server-side rate limits.",
                "Pair with an extract provider if you also need web_extract."))}


def _post_setup_python(spec: dict) -> None:
    """Enable one Python provider through the application dependency transaction."""
    import pm

    label = spec["label"]
    _print_info(f"    {spec['installing']}")
    try:
        pm.sync_venv([spec["extra"]], explicit=True)
    except (pm.InstallError, OSError, ValueError) as exc:
        _print_warning(f"    {label} install failed: {exc}")
        _info_lines("Retry with: noesia tools")
        return
    _print_success(f"    {label} dependencies ready. Restart Noesia to use them.")
    _info_lines(*spec["on_install"], *spec["always"])



def _post_setup_xai_grok() -> None:
    """xAI 系工具（TTS / STT / 视频 / x_search）共用的凭据引导：只用 XAI_API_KEY。"""
    if get_env_value("XAI_API_KEY"):
        _print_success("    xAI will use your existing XAI_API_KEY")
        return

    _print_info("    xAI needs an API key (console.x.ai).")
    try:
        from noesia_cli.setup import prompt as _setup_prompt
        from noesia_cli.config import save_env_value
    except Exception as exc:
        _print_warning(f"    Could not load setup helpers: {exc}")
        _info_lines("Run later: set XAI_API_KEY")
        return

    api_key = _setup_prompt("    xAI API key", password=True)
    if not api_key:
        _print_warning("    No API key provided. Run later: set XAI_API_KEY")
        _print_info("    xAI will remain inactive until XAI_API_KEY is configured.")
        return
    save_env_value("XAI_API_KEY", api_key)
    _print_success("    XAI_API_KEY saved")


def _xai_credentials_ready() -> bool:
    from noesia_cli.tools_config import _xai_credentials_present  # facade binding: tests patch it there
    return _xai_credentials_present()


# Credential-bootstrap post_setup keys -> "credentials present" predicate. These rows have no install
# side-effect; ``provider_readiness_status`` reports them ready/needs_auth from the auth store.
_POST_SETUP_AUTH_READY: dict = {
    "xai_grok": _xai_credentials_ready,
}


# post_setup key -> hook. Unknown keys are a silent no-op (callers validate against valid_post_setup_keys()).
_POST_SETUP_HOOKS: dict = {
    "lightpanda": _post_setup_lightpanda,
    "agent_browser": lambda: _post_setup_agent_browser("agent_browser"),
    "browserbase": lambda: _post_setup_agent_browser("browserbase"),
    "browser_use_cli": lambda: _ensure_browser_use_cli(verbose_hints=True),
    "camofox": _post_setup_camofox,
    "cua_driver": lambda: install_cua_driver(upgrade=False),
    "xai_grok": _post_setup_xai_grok,
    **{key: (lambda spec=spec: _post_setup_python(spec)) for key, spec in _PYTHON_POST_SETUP_HOOKS.items()},
}


def _run_post_setup(post_setup_key: str):
    """Run post-setup hooks for tools that need extra installation steps."""
    _POST_SETUP_HOOKS.get(post_setup_key, lambda: None)()


def valid_post_setup_keys() -> set[str]:
    """Return the set of post-setup keys declared by any visible provider (``TOOL_CATEGORIES`` plus
    plugin-registered providers). This is the allowlist ``post-setup`` and the dashboard endpoint
    validate against, so a caller cannot drive ``_run_post_setup`` with an arbitrary key."""
    from noesia_cli.tools_config import (
        TOOL_CATEGORIES, _plugin_browser_providers, _plugin_image_gen_providers,
        _plugin_video_gen_providers, _plugin_web_search_providers)

    keys: set[str] = set()
    for cat in TOOL_CATEGORIES.values():
        keys.update(ps for prov in cat.get("providers", []) if (ps := prov.get("post_setup")))
    for builder in (_plugin_web_search_providers, _plugin_image_gen_providers,
                    _plugin_video_gen_providers, _plugin_browser_providers):
        try:
            keys.update(ps for prov in builder() if (ps := prov.get("post_setup")))
        except Exception:  # pragma: no cover — defensive; plugins optional
            continue
    return keys


def run_post_setup_command(args) -> int:
    """``noesia tools post-setup <key>`` — non-interactive runner the dashboard spawns so the GUI can drive
    backend setup without re-implementing install logic. Exit code: 0 ok, 2 unknown key."""
    key = getattr(args, "post_setup_key", None)
    if not key:
        _print_error("Usage: noesia tools post-setup <key>")
        return 2
    valid = valid_post_setup_keys()
    if key not in valid:
        _print_error(f"Unknown post-setup key: {key!r}. Valid keys: {', '.join(sorted(valid)) or '(none)'}")
        return 2
    _print_info(f"Running post-setup hook: {key}")
    try:
        _run_post_setup(key)
    except Exception as exc:  # pragma: no cover — defensive
        _print_error(f"Post-setup failed: {exc}")
        return 1
    _print_success(f"Post-setup '{key}' complete")
    return 0


# post_setup_key -> predicate(): True when the install side-effect is already satisfied. Used by
# `_toolset_needs_configuration_prompt` to force provider setup when a no-key provider still needs a
# binary/dependency install (otherwise toggling the toolset on silently skips the hook). Only add an
# entry when the post_setup is the ONLY install side-effect for a no-key provider and the check is
# local, bounded, and import-light.
_POST_SETUP_INSTALLED: dict = {"cua_driver": lambda: _cua_driver_install_ready()}


def _post_setup_already_installed(post_setup_key: str) -> bool:
    """Return True when the post_setup install side-effect is satisfied (or no check is registered)."""
    predicate = _POST_SETUP_INSTALLED.get(post_setup_key)
    try:
        return predicate is None or bool(predicate())
    except Exception:
        return True


def _module_installed(module_name: str) -> bool:
    """Cheap importable-without-importing check (no heavy side effects)."""
    import importlib.util
    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def _agent_browser_installed() -> bool:
    """True when everything ``_run_post_setup("agent_browser")`` installs is present: the agent-browser CLI
    *and* the Chromium build it drives (or the Lightpanda engine, which needs no Chromium), so "Run
    setup" flips to installed only when re-running it would be a no-op."""
    from noesia_cli.browser_readiness import local_browser_runnable

    return local_browser_runnable()


def _camofox_installed() -> bool:
    """Readiness belongs to the configured external server, not root node_modules."""
    from tools.browser_camofox import check_camofox_available
    return check_camofox_available()


def _lightpanda_installed() -> bool:
    """True when a lightpanda binary is on PATH or in a known install dir."""
    try:
        from tools.browser_lightpanda import find_lightpanda_binary
        return find_lightpanda_binary() is not None
    except Exception:
        return False


def _cloud_agent_browser_installed() -> bool:
    """Installed-check for the ``browserbase`` hook: cloud providers host their own Chromium, so
    presence of the agent-browser CLI is the whole contract."""
    from noesia_cli.browser_readiness import has_agent_browser
    return has_agent_browser()


# post_setup_key -> predicate(): True when the install side-effect is satisfied. Used by
# ``provider_readiness_status`` to mark a keyless post_setup row "ready" vs "needs_setup"; mirrors the
# installed-checks the hooks perform. Credential bootstraps (``xai_grok``) are absent —
# they live in ``_POST_SETUP_AUTH_READY`` as auth checks. Late-bound lambdas so tests can monkeypatch the underlying predicates.
_POST_SETUP_READY: dict = {
    **{key: (lambda m=spec["module"]: _module_installed(m)) for key, spec in _PYTHON_POST_SETUP_HOOKS.items()},
    "agent_browser": lambda: _agent_browser_installed(),
    "browserbase": lambda: _cloud_agent_browser_installed(),
    "camofox": lambda: _camofox_installed(),
    "lightpanda": lambda: _lightpanda_installed(),
    "cua_driver": lambda: _cua_driver_install_ready()}
