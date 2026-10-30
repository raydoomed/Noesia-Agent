"""Local browser readiness probes shared by setup and post-setup surfaces.

Split out of the former subscription module: these answer "would this machine actually run a
browser?" from local state only, with no account or entitlement lookup.
"""


def has_agent_browser() -> bool:
    """True when the agent-browser CLI resolves on this machine."""
    # Read the runtime's choice; a broken resolver is not permission to
    # advertise an unchecked binary through a second discovery ladder.
    try:
        from tools.browser_tool_install import _find_agent_browser
        _find_agent_browser(validate=False)
    except (ImportError, OSError):
        return False
    return True


def local_browser_runnable() -> bool:
    """True when the *local* browser backend would actually start: the CLI must be present AND a
    Chromium build on disk (else agent-browser hangs until the command timeout) unless the Lightpanda
    engine is selected. Mirrors the local-mode tail of tools.browser_tool_install.check_browser_requirements."""
    if not has_agent_browser():
        return False
    try:
        from tools.browser_tool_install import _chromium_installed
        from tools.browser_tool_lightpanda_fallback import _using_lightpanda_engine
    except Exception:
        return True  # runtime probe unavailable: fall back to binary presence rather than crashing
    return _using_lightpanda_engine() or _chromium_installed()
