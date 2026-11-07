"""TUI agent_factory seam; functions bind to the server namespace at registration."""

from __future__ import annotations

from .method_ctx import bind_module

def _env_model_seed() -> str:
    """The launch-scoped model seed (``noesia --tui -m``, hosted provisioning); "" when unset."""
    return (os.environ.get("NOESIA_MODEL", "") or os.environ.get("NOESIA_INFERENCE_MODEL", "")).strip()


def _resolve_model() -> str:
    if env := _env_model_seed():
        return env
    m = _load_cfg().get("model", "")
    if isinstance(m, dict):
        return str(m.get("default", "") or "").strip()
    if isinstance(m, str) and m:
        return m.strip()
    with contextlib.suppress(Exception):
        from noesia_cli.models import get_preferred_silent_default_model
        return get_preferred_silent_default_model()
    return ""


def _resolve_session_platform() -> str:
    """``NOESIA_DESKTOP=1`` without ``NOESIA_DESKTOP_TERMINAL`` → "desktop" (chat panel; the agent then
    suggests TUI-only slash commands), else "tui" (embedded terminal pane or standalone ``noesia --tui``)."""
    desktop = is_truthy_value(os.environ.get("NOESIA_DESKTOP"))
    return "desktop" if desktop and not is_truthy_value(os.environ.get("NOESIA_DESKTOP_TERMINAL")) else "tui"


def _resolve_session_source(explicit: str | None) -> str:
    """Session DB ``source``: an explicit caller value (plugin session tagged ``"telegram"``) is never
    rewritten; only empty/None falls back to the env-resolved platform."""
    return explicit or _resolve_session_platform()


def _resolve_agent_platform(source: str | None) -> str:
    return _resolve_session_source(source)


def _config_model_target() -> tuple[str, str]:
    """(model, provider) selected by config.yaml — and ONLY config: the NOESIA_MODEL launch seed fed into
    the per-turn sync would be replayed as a /model switch and persisted globally, or pin the session so
    dashboard/CLI model changes never reach an open chat. Empty model = "no preference" → no-op sync."""
    cfg_model = _load_cfg().get("model")
    if isinstance(cfg_model, dict):
        provider = str(cfg_model.get("provider") or "").strip()
        return str(cfg_model.get("default", "") or "").strip(), "" if provider.lower() == "auto" else provider
    return (cfg_model.strip() if isinstance(cfg_model, str) else ""), ""


def _resolve_startup_runtime() -> tuple[str, str | None]:
    model = _resolve_model()
    if explicit_provider := os.environ.get("NOESIA_TUI_PROVIDER", "").strip():
        return model, explicit_provider
    if not (explicit_model := _env_model_seed()):
        return model, None
    with contextlib.suppress(Exception):
        from noesia_cli.model_switch import resolve_startup_model_route
        from noesia_cli.models import detect_static_provider_for_model
        full_cfg = _load_cfg()
        cfg = full_cfg.get("model") or {}
        current_provider = ((str(cfg.get("provider") or "").strip().lower() if isinstance(cfg, dict) else "")
                            or os.environ.get("NOESIA_INFERENCE_PROVIDER", "").strip().lower() or "auto")
        # Same owner as NoesiaCLI/oneshot: ``custom:<name>:<model>`` selects that provider (#73943).
        if route := resolve_startup_model_route(
                explicit_model, current_provider=current_provider,
                user_providers=full_cfg.get("providers"), custom_providers=full_cfg.get("custom_providers")):
            return route.model, route.provider
        if detected := detect_static_provider_for_model(explicit_model, current_provider):
            provider, detected_model = detected
            return detected_model, provider
    return model, None


def _is_routable_provider(provider: str) -> bool:
    with contextlib.suppress(Exception):
        from noesia_cli.runtime_provider import is_routable_provider
        return is_routable_provider(provider)
    return False


def _overrides_have_routable_provider(overrides: dict) -> bool:
    """Whether persisted runtime overrides still name a routable provider (renamed/removed → "Unknown
    provider" at agent init). Empty = NOT routable, so the caller falls back to the session's picked model."""
    provider = str(overrides.get("provider_override") or "").strip()
    if not provider:
        provider = str((overrides.get("model_override") or {}).get("provider") or "").strip()
    return bool(provider) and _is_routable_provider(provider)


def _parse_model_config(raw, *, quiet: bool = False) -> dict:
    """A row's ``model_config`` (dict or JSON text) as a dict; ``{}`` when absent/invalid."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            if not quiet:
                raise
            logger.debug("failed to parse stored session model_config", exc_info=True)
    return {}


def _row_follows_profile(row: dict | None) -> bool:
    """Whether a stored row is a canonical Bot Chat whose runtime follows the member profile's config.
    Identity is the persisted ``follow_profile_config`` marker; the bare title compare stays ONLY here as
    the legacy fallback for rows written before the marker existed."""
    if not row:
        return False
    model_config = _parse_model_config(row.get("model_config"), quiet=True)
    return bool(model_config.get("follow_profile_config") or str(row.get("title") or "").strip() == "Bot Chat")


def _stored_session_runtime_overrides(row: dict | None) -> dict:
    """Runtime fields persisted with a stored session (model column, ``billing_provider``, JSON ``model_config``):
    resume restores the model/provider/reasoning THAT chat used, not the global pick. Plugin-owned Bot-Mode
    sessions normally rebuild from the member profile's CURRENT config (a stale provider pin left room bots
    "out of Noesia credits" after a profile switch). A canonical Bot Chat may instead restore an explicit
    composer pick while the profile model it diverged from remains unchanged."""
    if not row:
        return {}
    model_config = _parse_model_config(row.get("model_config"), quiet=True)
    _row_title = str(row.get("title") or "").strip()
    room_plumbing = model_config.get("room_plumbing") or (row.get("hidden") and _row_title.startswith("Group:"))
    composer_profile = model_config.get("composer_override_profile")
    composer_profile_matches = isinstance(composer_profile, dict) and (
        str(composer_profile.get("model") or "").strip(),
        str(composer_profile.get("provider") or "").strip(),
    ) == _config_model_target()
    if room_plumbing or (_row_follows_profile(row) and not composer_profile_matches):
        return {}
    overrides: dict = {}
    model = str(row.get("model") or model_config.get("model") or "").strip()
    # Canonical route reader shared with CLI --resume: nested ``gateway_runtime`` (the route the messaging
    # gateway last ran) before the TUI's top-level keys, then a routable ``billing_provider`` (#125942).
    from noesia_state import SessionDB
    route = SessionDB.session_gateway_runtime(row)
    provider, base_url, api_mode = (str(route.get(k) or "").strip() for k in ("provider", "base_url", "api_mode"))
    service_tier = str(model_config.get("service_tier") or "").strip()
    reasoning_config = model_config.get("reasoning_config")
    from noesia_cli.runtime_provider import is_foreign_provider_endpoint
    if is_foreign_provider_endpoint(provider, base_url):
        # The endpoint and its wire belong to the provider this chat left; resolve the stored one's own.
        base_url = api_mode = ""
    # Heal a stale provider persisted by an older build (renamed/removed custom provider → "Unknown provider"):
    # recover ``custom:<name>`` from the stored base_url, then from the entry serving the model; else drop it.
    if provider and not _is_routable_provider(provider):
        healed = None
        try:
            from noesia_cli.runtime_provider import canonical_custom_identity
            healed = canonical_custom_identity(base_url=base_url or None, model=model or None)
        except Exception:
            logger.debug("custom provider identity recovery failed", exc_info=True)
        if healed:
            logger.info("healed stale session provider %r to %r", provider, healed)
            provider = healed
            base_url = ""  # the healed identity owns a registered endpoint; the snapshot URL must not override it
        else:
            # The route is gone with its endpoint and wire: on the default provider they would send
            # that provider's credentials to the stored URL.
            provider = base_url = api_mode = ""
    if model:
        # Same dict-shaped override live /model switches use, so a DB-restored session keeps custom endpoint
        # metadata across resume and rebuilds (/new). Raw api_key is never persisted/restored.
        overrides["model_override"] = {
            "model": model, "provider": provider or None, "base_url": base_url or None, "api_mode": api_mode or None}
    if provider:
        overrides["provider_override"] = provider
    if isinstance(reasoning_config, dict):
        overrides["reasoning_config_override"] = reasoning_config
    if service_tier:  # None = "inherit the profile" at _make_agent; "" = real override "no priority tier"
        overrides["service_tier_override"] = "" if service_tier.lower() == "normal" else service_tier
    return overrides


def _runtime_model_config(agent, existing: dict | None = None) -> dict:
    """Merge the agent's CURRENT runtime identity onto the row's persisted ``model_config``. Falsy agent
    attributes DELETE the key rather than skip the write: resume reads provider/endpoint from this JSON
    (model column written separately), so a stale provider would route the resumed chat to the wrong endpoint."""
    config = dict(existing or {})
    from agent.voice_turn_route import session_runtime_view
    agent = session_runtime_view(agent)
    attr = lambda k: str(getattr(agent, k, "") or "").strip()
    model, provider, base_url = attr("model"), attr("provider"), attr("base_url")
    if provider.lower() == "custom":
        # ``agent.provider`` resolves every named custom entry to the literal "custom", losing the entry
        # identity (api_key is never persisted): recover ``custom:<name>`` from the endpoint URL.
        try:
            from noesia_cli.runtime_provider import canonical_custom_identity
            provider = canonical_custom_identity(base_url=base_url, model=model or None) or provider
        except Exception:
            logger.debug("custom provider identity lookup failed", exc_info=True)
    reasoning_config = getattr(agent, "reasoning_config", None)
    live = {
        "model": model, "provider": provider, "base_url": base_url, "api_mode": attr("api_mode"),
        # An empty dict is still a real (present) reasoning config.
        "reasoning_config": reasoning_config if isinstance(reasoning_config, dict) else None,
        "service_tier": getattr(agent, "service_tier", None),
    }
    for key, value in live.items():
        if value or isinstance(value, dict):
            config[key] = value
        else:
            config.pop(key, None)
    return config


def _persist_live_session_runtime(session: dict | None) -> None:
    """Persist active session runtime so future resumes restore the same footer."""
    live = _live_session_agent_db(session)
    if live is None:
        return
    agent, session_key, db = live
    try:
        row = db.get_session(session_key) or {}
        model_config = _runtime_model_config(agent, _parse_model_config(row.get("model_config")))
        if isinstance(composer_profile := session.get("composer_override_profile"), dict):
            model_config["composer_override_profile"] = composer_profile
        elif "composer_override_profile" in session:
            model_config.pop("composer_override_profile", None)
        if (tier_override := session.get("create_service_tier_override")) is not None:
            # agent.service_tier is None for explicit normal; without this the distinction is erased on every persist.
            model_config["service_tier"] = tier_override or "normal"
        model = str(model_config.get("model") or "").strip()
        if hasattr(db, "update_session_meta"):
            db.update_session_meta(session_key, json.dumps(model_config), model or None)
        elif model and hasattr(db, "update_session_model"):
            db.update_session_model(session_key, model)
    except Exception:
        logger.debug("failed to persist live session runtime", exc_info=True)

def _load_approval_mode() -> str:
    """Effective ``approvals.mode`` via the gate's own ``_get_approval_mode`` (a raw re-read missed the
    managed overlay and ``${VAR}`` expansion)."""
    from tools.approval_context import _get_approval_mode
    mode = _get_approval_mode()
    return mode if mode in _APPROVAL_MODES else "manual"


def _coerce_statusbar(raw) -> str:
    if raw is False:
        return "off"
    return s if isinstance(raw, str) and (s := raw.strip().lower()) in _STATUSBAR_MODES else "top"


_MOUSE_TRACKING_ALIASES = {
    "0": "off", "1": "all", "all": "all", "any": "all", "button": "buttons", "buttons": "buttons",
    "click": "buttons", "false": "off", "full": "all", "no": "off", "off": "off", "on": "all",
    "scroll": "wheel", "true": "all", "wheel": "wheel", "yes": "all",
}


def _display_mouse_tracking(display: dict) -> str:
    """display.mouse_tracking → ``off|wheel|buttons|all`` (bools: True → all, False → off); ``wheel`` (DEC
    1000+1006) is the tmux-friendly subset without hover events. Legacy ``tui_mouse`` only when ``mouse_tracking`` is absent."""
    if not isinstance(display, dict):
        return "all"
    raw = display.get("mouse_tracking") if "mouse_tracking" in display else display.get("tui_mouse", True)
    if isinstance(raw, str):
        return _MOUSE_TRACKING_ALIASES.get(raw.strip().lower(), "all")
    return "off" if raw is False or raw == 0 else "all"


def _load_reasoning_config(model: str = "") -> dict | None:
    """Via the shared chokepoint :func:`noesia_constants.resolve_reasoning_config` (per-model override >
    global ``agent.reasoning_effort``; YAML False = disabled).

    Closes #21256.
    """
    from noesia_constants import resolve_reasoning_config
    return resolve_reasoning_config(_load_cfg(), model)


def _load_service_tier() -> str | None:
    from agent.fast_mode import parse_service_tier

    return parse_service_tier((_load_cfg().get("agent") or {}).get("service_tier", ""))


def _load_provider_routing() -> dict:
    """OpenRouter ``provider_routing`` prefs (gateway/CLI parity — without them OpenRouter picks an effectively random provider)."""
    with contextlib.suppress(Exception):
        return _load_cfg().get("provider_routing", {}) or {}
    return {}


def _load_show_reasoning() -> bool:
    # Fallback True — keep in sync with DEFAULT_CONFIG display.show_reasoning (no DEFAULT_CONFIG merge here).
    return bool(_display_cfg().get("show_reasoning", True))


def _load_memory_notifications() -> str:
    """``display.memory_notifications`` (``off`` / ``on`` default / ``verbose``; bool normalized) — gates the
    "💾 Self-improvement review" summary (gateway/CLI parity)."""
    raw = _display_cfg().get("memory_notifications")
    if isinstance(raw, bool):
        return "on" if raw else "off"
    return str(raw).lower() if raw else "on"


_TOOL_PROGRESS_MODES = frozenset({"off", "new", "all", "verbose"})


def _load_tool_progress_mode() -> str:
    env = os.environ.get("NOESIA_TUI_TOOL_PROGRESS", "").strip().lower()
    if env in _TOOL_PROGRESS_MODES:
        return env
    raw = _display_cfg().get("tool_progress", "all")
    if isinstance(raw, bool):
        return "all" if raw else "off"
    mode = str(raw or "all").strip().lower()
    return mode if mode in _TOOL_PROGRESS_MODES else "all"


def _gui_surface_toolsets(platform: str) -> set[str]:
    """Toolsets that exist because of the CLIENT (off ``_NOESIA_CORE_TOOLS``; this is the one gate).
    ``platform`` is the SESSION's source, never a process env var: the desktop may drive a URL/cloud
    backend where ``NOESIA_DESKTOP`` is unset (AGENTS.md surface rule)."""
    from toolsets import CLIENT_SURFACE_TOOLSETS
    return set(CLIENT_SURFACE_TOOLSETS) if platform == "desktop" else {"project"}


def _with_session_toolsets(selection, platform: str) -> list[str]:
    """*selection* plus the client surface's toolsets the session carries whatever its config says.

    The fold-in happens after ``_get_platform_tools`` already subtracted ``agent.disabled_toolsets``,
    so the same subtraction is applied to the fold-in itself — otherwise ``disabled_toolsets:
    [project]`` is a no-op on desktop/TUI, the only surfaces where the client toolsets exist
    (#54433). ``desktop_ui`` is kept regardless: it is the client's own control surface, not a
    model toolset."""
    fold_in = _gui_surface_toolsets(platform) - set(selection)
    disabled = set(_load_disabled_toolsets() or [])
    if disabled:
        fold_in -= disabled - {"desktop_ui"}
    return [*selection, *sorted(fold_in)]


def _tui_notice(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _resolve_explicit_toolsets(explicit: list[str], validate_toolset) -> list[str] | None | bool:
    """Resolve a NOESIA_TUI_TOOLSETS pin: list, None for "all", False when nothing was valid."""
    built_in = [name for name in explicit if validate_toolset(name)]
    unresolved = [name for name in explicit if name not in built_in]
    if unresolved:
        try:
            from noesia_cli.plugins import discover_plugins
            discover_plugins()
            plugin_valid = [name for name in unresolved if validate_toolset(name)]
        except Exception:
            plugin_valid = []
        built_in.extend(plugin_valid)
        unresolved = [name for name in unresolved if name not in plugin_valid]
    if any(name in {"all", "*"} for name in built_in):
        if ignored := [name for name in explicit if name not in {"all", "*"}]:
            _tui_notice(f"[tui] NOESIA_TUI_TOOLSETS=all enables every toolset; ignoring additional entries: {', '.join(ignored)}")
        return None
    if not unresolved:
        return built_in
    try:  # (enabled, disabled) MCP server names from raw config; both empty on any failure
        from noesia_cli.config import read_raw_config
        from tools.mcp_tool_common import mcp_server_enabled
        raw_cfg = read_raw_config()
        mcp_servers = raw_cfg.get("mcp_servers") if isinstance(raw_cfg.get("mcp_servers"), dict) else {}
        mcp_names, mcp_disabled = set(), set()
        for name, server_cfg in mcp_servers.items():
            if isinstance(server_cfg, dict):
                on = mcp_server_enabled(server_cfg)
                (mcp_names if on else mcp_disabled).add(str(name))
    except Exception:
        mcp_names, mcp_disabled = set(), set()
    mcp_valid = [name for name in unresolved if name in mcp_names]
    disabled = [name for name in unresolved if name in mcp_disabled]
    unknown = [name for name in unresolved if name not in mcp_names and name not in mcp_disabled]
    if unknown:
        _tui_notice(f"[tui] ignoring unknown NOESIA_TUI_TOOLSETS entries: {', '.join(unknown)}")
    if disabled:
        _tui_notice("[tui] ignoring disabled MCP servers in NOESIA_TUI_TOOLSETS "
                    f"(set enabled: true in config.yaml to use): {', '.join(disabled)}")
    return (built_in + mcp_valid) or False


def _load_enabled_toolsets(platform: str | None = None) -> list[str] | None:
    """The agent's toolsets for this session (None = all): an explicit NOESIA_TUI_TOOLSETS pin; else the
    coding posture (coding_context collapses to coding toolset + enabled MCP servers in a code workspace);
    else the configured CLI toolsets. Client-surface toolsets fold in here — only this surface can answer them.
    An explicitly saved EMPTY list (``platform_toolsets.cli: []`` that resolves to nothing) is a
    zero-tool state and returns [] — never None, which would mean unrestricted (#82010)."""
    session_platform = platform or _resolve_session_platform()
    explicit = [item.strip() for item in os.environ.get("NOESIA_TUI_TOOLSETS", "").split(",") if item.strip()]
    fallback_notice = None
    if not explicit:
        with contextlib.suppress(Exception):
            from agent.coding_context import coding_selection
            selection = coding_selection(platform=session_platform)
            if selection is not None:
                return sorted(_with_session_toolsets(selection, session_platform))
    try:
        from toolsets import validate_toolset
    except Exception:
        validate_toolset = None
    if explicit and validate_toolset is not None:
        resolved = _resolve_explicit_toolsets(explicit, validate_toolset)
        if resolved is not False:
            # An operator pin replaces the surface fold-in.
            return resolved
        fallback_notice = "[tui] no valid NOESIA_TUI_TOOLSETS entries; using configured CLI toolsets"
    try:
        from noesia_cli.config import load_config
        from noesia_cli.tools_config import _get_platform_tools, _platform_toolsets_explicitly_saved
        cfg = load_config()
        # include_default_mcp_servers=True is the runtime variant (the agent must be able to call
        # default MCP servers); the config-editing variant would silently drop MCP tools from the TUI.
        # Passing ``False`` here is the config-editing variant — used when we need to persist a toolset list
        # without baking in implicit MCP defaults. Using the wrong variant at agent creation time makes MCP
        # tools silently missing from the TUI. See PR #3252 for the original design split.
        enabled = _get_platform_tools(cfg, "cli", include_default_mcp_servers=True)
        if fallback_notice is not None:
            _tui_notice(fallback_notice)
        if enabled:
            return sorted(_with_session_toolsets(enabled, session_platform))
        # An explicitly saved list that resolves to nothing (``platform_toolsets.cli: []`` with no
        # enabled MCP servers) is an explicit zero-tool state, not "no filter": fail closed with []
        # instead of None (#82010). The client-surface fold-in must not resurrect tools the user
        # explicitly switched off, so [] is returned bare. Only an ABSENT/unset key keeps the
        # None = "no restriction" default below.
        if _platform_toolsets_explicitly_saved(cfg, "cli"):
            return []
        return None
    except Exception:
        if fallback_notice is not None:
            _tui_notice("[tui] no valid NOESIA_TUI_TOOLSETS entries and configured CLI toolsets could not be loaded; enabling all toolsets")
        return None


def _load_disabled_toolsets() -> list[str] | None:
    """``agent.disabled_toolsets`` from config.yaml, or ``None``.

    The classic CLI (``cli_init_mixin``) and the messaging gateway both forward this list to
    AIAgent, where ``get_tool_definitions`` strips the named toolsets even out of composite
    defaults like ``noesia-cli`` (#17309). The desktop/TUI gateway historically dropped it, so
    e.g. ``disabled_toolsets: [browser]`` silently had no effect on Desktop — the only consumer
    was ``_get_platform_tools``'s name-level subtraction, which can't reach inside a composite
    default toolset (#44499).
    """
    try:
        from agent.skill_utils import parse_config_string_list

        from noesia_cli.config import load_config

        agent_cfg = load_config().get("agent") or {}
        disabled = parse_config_string_list(agent_cfg.get("disabled_toolsets"))
        return [str(ts) for ts in disabled] or None
    except Exception:
        return None


def _session_tool_progress_mode(sid: str) -> str:
    return str(_sessions.get(sid, {}).get("tool_progress_mode", "all") or "all")


def _session_verbose(sid: str) -> bool:
    return _session_tool_progress_mode(sid) == "verbose"


def _session_show_reasoning(sid: str) -> bool:
    """Session display flag. Missing means the config default, not hidden."""
    session = _sessions.get(sid) or {}
    if "show_reasoning" in session:
        return bool(session["show_reasoning"])
    return _load_show_reasoning()


def _tool_progress_enabled(sid: str) -> bool:
    return _session_tool_progress_mode(sid) != "off"


_TOOL_LIFECYCLE_UI_TOOLS = frozenset({
    "clarify", "manage_connections", "setup_mcp",
    "image_generate", "manage_catalog", "delegate_task", "setup_beat", "set_theme",
    # File edits are the turn's deliverable — the diff card the user reviews.
    "edit_file", "patch", "write_file",
})


def _tool_lifecycle_required_for_ui(name: str) -> bool:
    """Interactive UI / card surfaces, not optional chrome.

    Desktop renders these from the tool-call part itself, so suppressing the
    lifecycle hides the turn's deliverable entirely (`isCardTool` /
    `isFileEditTool` must stay in sync with `_TOOL_LIFECYCLE_UI_TOOLS`): clarify / connection cards are
    consent surfaces, image_generate / manage_catalog / delegate_task draw the
    thing the user asked for, and file edits are the diff the user reviews.
    The start and complete guards both consult this set, so a card's
    `tool.complete` can never arrive without its `tool.start`.
    """
    return name in _TOOL_LIFECYCLE_UI_TOOLS

def _resolve_runtime_with_fallback(resolve_kwargs: dict | None = None) -> _RuntimeFallbackResolution:
    """Resolve the primary runtime or one complete provider/model fallback. Provider-only fallback entries
    are skipped so the unavailable primary model can never leak into a different runtime."""
    from noesia_cli.auth import AuthError
    from noesia_cli.runtime_provider import resolve_runtime_provider
    try:
        return _RuntimeFallbackResolution(resolve_runtime_provider(**(resolve_kwargs or {})), None, False)
    except AuthError as primary_exc:
        for entry in _load_fallback_model() or []:
            fb_provider = str(entry.get("provider") or "").strip() if isinstance(entry, dict) else ""
            fb_model = str(entry.get("model") or "").strip() if isinstance(entry, dict) else ""
            if not fb_provider or not fb_model:
                continue
            try:
                from noesia_cli.fallback_config import effective_runtime_provider, resolve_entry_api_key
                fb_kwargs: dict = {"requested": fb_provider, "target_model": fb_model,
                                   **({"explicit_base_url": entry["base_url"]} if entry.get("base_url") else {})}
                if fb_api_key := resolve_entry_api_key(entry):
                    fb_kwargs["explicit_api_key"] = fb_api_key
                runtime = resolve_runtime_provider(**fb_kwargs)
                # Named custom entries resolve to the bare "custom" billing class; keep the configured
                # identity so the session/UI shows the provider name, matching the manual-switch path (#98739).
                runtime["provider"] = effective_runtime_provider(entry, runtime)
                from noesia_cli.auth import primary_failure_wording
                logging.getLogger(__name__).warning(
                    "Primary %s (%s), falling back to %s model %s",
                    primary_failure_wording(primary_exc)[0], primary_exc, fb_provider, fb_model)
                return _RuntimeFallbackResolution(runtime, fb_model, True)
            except Exception:
                continue
        raise


def _resolve_agent_model_runtime(model_override, provider_override) -> tuple[str, dict]:
    """(model, runtime) for a new agent; a per-session override (/model switch or a resumed row's persisted
    runtime) wins over global config/env. Older rows stored the resolved provider "custom" (no named entry
    matches) — recover the identity from the persisted base_url or the rebuild fails "No LLM provider
    configured". Persisted base_url/api_key/api_mode are honored only for the original runtime, never a fallback."""
    if isinstance(model_override, dict) and model_override.get("model"):
        model = str(model_override.get("model") or "")
        requested_provider = model_override.get("provider") or provider_override or None
        override_base_url = model_override.get("base_url")
        resolve_kwargs = {}
        if str(requested_provider or "").strip().lower() == "custom":
            from noesia_cli.runtime_provider import canonical_custom_identity
            if recovered := canonical_custom_identity(base_url=override_base_url or None, model=model or None):
                requested_provider = recovered
            if override_base_url:
                # Failing identity recovery, still hand base_url to the direct-alias branch so pool/env credentials resolve.
                resolve_kwargs["explicit_base_url"] = override_base_url
        resolve_kwargs.update(requested=requested_provider, target_model=model or None)
        overrides = {k: model_override.get(k) for k in ("base_url", "api_key", "api_mode")}
    else:
        model, requested_provider = _resolve_startup_runtime()
        if isinstance(model_override, str) and model_override:
            model = model_override
        if provider_override:
            requested_provider = provider_override
        resolve_kwargs = {"requested": requested_provider, "target_model": model or None}
        overrides = {}
    resolution = _resolve_runtime_with_fallback(resolve_kwargs)
    if resolution.used_fallback:
        if not resolution.selected_model:
            raise RuntimeError("Auth fallback resolved without a model")
        # Same pre-agent switch the messaging gateway surfaces (#74349); _make_agent pops it onto the
        # agent's one-shot notice so the TUI/Desktop user sees which provider actually answered.
        from noesia_cli.fallback_config import pre_agent_fallback_notice
        # requested_provider=None means resolve_runtime_provider read the persisted config provider;
        # ``model: <id>`` (string shorthand) names no provider.
        cfg_model = _load_cfg().get("model")
        primary_provider = requested_provider or (cfg_model.get("provider") if isinstance(cfg_model, dict) else None)
        resolution.runtime["_fallback_notice"] = pre_agent_fallback_notice(
            primary_provider, model, resolution.runtime.get("provider"), resolution.selected_model)
        return resolution.selected_model, resolution.runtime
    if resolution.runtime.get("source") == "local-runtime":
        # Live supervisor beat any persisted loopback URL for this identity.
        overrides.pop("base_url", None)
    resolution.runtime.update({k: v for k, v in overrides.items() if v})
    if any(overrides.values()):
        _rederive_per_model_route(model, resolution.runtime)
    if not str(model or "").strip():
        raise RuntimeError("还没有启用模型：打开「设置 → 模型设置」，选一个模型再点保存并启用。")
    return model, resolution.runtime


def _rederive_per_model_route(model: str, runtime: dict) -> None:
    """A row's persisted api_mode/base_url were written for whichever model the session last ran. Providers
    that pick the wire per model (OpenCode Zen/Go, Copilot, Noesia) must re-derive both from the target model,
    or a resumed opencode-go session keeps a MiniMax-era anthropic_messages route (and its /v1-stripped or
    other-family relay URL) for a chat_completions model like deepseek-v4-flash-vision-exp (#96066)."""
    from noesia_cli.model_switch import model_derived_api_mode
    from noesia_cli.models import normalize_opencode_base_url
    provider = str(runtime.get("requested_provider") or runtime.get("provider") or "")
    api_mode = model_derived_api_mode(provider, model)
    if api_mode is None:
        return
    runtime["api_mode"] = api_mode
    runtime["base_url"] = normalize_opencode_base_url(provider, api_mode, runtime.get("base_url"))


def _startup_system_prompt(cfg: dict, task_id: str) -> str:
    """Config ephemeral system prompt + NOESIA_TUI_SKILLS preload block. Hard-fails only when EVERY requested
    skill is missing (cli.py parity): a typo'd name must not auto-block the Kanban task."""
    from noesia_cli.config import resolve_ephemeral_system_prompt_from_config
    system_prompt = resolve_ephemeral_system_prompt_from_config(cfg)
    startup_skills = _parse_tui_skills_env()
    if not startup_skills:
        return system_prompt
    from agent.skill_commands import build_preloaded_skills_prompt, format_missing_skills
    skills_prompt, loaded_skills, missing_skills = build_preloaded_skills_prompt(startup_skills, task_id=task_id)
    if missing_skills:
        if not loaded_skills:
            raise ValueError(format_missing_skills(missing_skills))
        logger.warning("Skipping %s. Continuing with: %s. List available skills with `noesia skills list`.",
                       format_missing_skills(missing_skills), ", ".join(loaded_skills))
    if skills_prompt:
        system_prompt = "\n\n".join(part for part in (system_prompt, skills_prompt) if part).strip()
    return system_prompt


def _make_agent(
    sid: str, key: str, session_id: str | None = None, session_db=None,
    model_override: dict | str | None = None, provider_override: str | None = None,
    reasoning_config_override: dict | None = None, service_tier_override: str | None = None,
    platform_override: str | None = None, context_cwd_is_launch_artifact: bool | None = None,
    cwd_override: str | None = None, auth_user_id: str | None = None):
    """Gateway front end of ``agent.agent_factory.build_agent``; only transport state lives here."""
    from agent.agent_factory import build_agent
    with _sessions_lock:
        session = _sessions.get(sid)
    platform = _resolve_agent_platform(platform_override)
    return build_agent(
        key, session_id=session_id, session_db=session_db if session_db is not None else _get_db(),
        enabled_toolsets=_load_enabled_toolsets(platform), callbacks=_agent_cbs(sid), session=session,
        model_override=model_override, provider_override=provider_override,
        reasoning_config_override=reasoning_config_override, service_tier_override=service_tier_override,
        platform_override=platform, context_cwd_is_launch_artifact=context_cwd_is_launch_artifact,
        cwd_override=cwd_override, auth_user_id=auth_user_id,
        pass_session_id=is_truthy_value(os.environ.get("NOESIA_TUI_PASS_SESSION_ID")))


def register(server):
    bind_module(globals(), server)
