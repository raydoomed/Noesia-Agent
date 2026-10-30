"""Targets of ``manage_connections``: local MCP servers from the bundled catalog."""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

MCP_ACTIONS = ("install", "enable", "authorize")
ALL_ACTIONS = MCP_ACTIONS

_TARGET_FIELDS = frozenset({"name", "mcp"})


def normalize_targets(raw: Any) -> Tuple[List[str], Optional[str]]:
    """The MCP server names a call names, in order, and an error string when the list is malformed."""
    if raw is None:
        return [], None
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        return [], "'connectors' must be a list of {name, mcp} objects."
    mcp: List[str] = []
    for item in raw:
        if isinstance(item, dict):
            unknown = sorted(set(item) - _TARGET_FIELDS)
            if unknown:
                return [], (
                    f"unknown target field(s) {', '.join(unknown)}: a target is "
                    "{'name': '<server>', 'mcp': true}. Transport, URLs and credentials come "
                    "from the catalog manifest, never from the call."
                )
            name = str(item.get("name") or "").strip().lower()
            is_mcp = bool(item.get("mcp", False))
        else:
            name, is_mcp = str(item or "").strip().lower(), False
        if not name:
            return [], "every target needs a non-empty 'name'."
        if not is_mcp:
            return [], (
                f"'{name}' is not an MCP server: every target must carry \"mcp\": true, e.g. "
                "[{\"name\": \"linear\", \"mcp\": true}]."
            )
        if name not in mcp:
            mcp.append(name)
    return mcp, None


def validate_action(action: str, mcp: list[str]) -> Optional[str]:
    if action not in ALL_ACTIONS:
        return (
            f"action must be one of {', '.join(ALL_ACTIONS)}. Every one of them applies to local "
            "MCP servers (targets {\"name\": ..., \"mcp\": true})."
        )
    if not mcp:
        return (
            f"'{action}' requires 'connectors': the MCP server name(s), e.g. "
            "[{\"name\": \"linear\", \"mcp\": true}]."
        )
    return None


def catalog_names() -> set[str]:
    try:
        from noesia_cli.mcp_catalog import list_catalog

        return {e.name for e in list_catalog()}
    except Exception as exc:
        logger.debug("MCP catalog for the routing check failed: %s", exc)
        return set()
