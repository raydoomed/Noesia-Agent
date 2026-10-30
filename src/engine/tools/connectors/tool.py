#!/usr/bin/env python3
"""Connection lifecycle tool for local MCP servers from the bundled catalog."""

from typing import Any, Callable, Dict, Optional

from tools.connectors.catalog_tool import MANAGE_CATALOG_SCHEMA, manage_catalog
from tools.connectors.mcp import run_mcp_operation
from tools.connectors.targets import ALL_ACTIONS, MCP_ACTIONS, normalize_targets, validate_action
from tools.registry import registry, tool_error


def manage_connections(
    args: dict[str, Any],
    *,
    mcp_backend: Optional[Any] = None,
    session_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    connection_callback: Optional[Callable[[dict[str, Any]], Optional[str]]] = None,
    card_surface: bool = False,
) -> str:
    action = str(args.get("action") or "status").strip().lower()
    mcp_targets, target_error = normalize_targets(args.get("connectors"))
    if target_error:
        return tool_error(target_error)
    action_error = validate_action(action, mcp_targets)
    if action_error:
        return tool_error(action_error)

    return run_mcp_operation(
        mcp_targets, action, backend=mcp_backend,
        connection_callback=connection_callback, session_id=session_id, tool_call_id=tool_call_id,
    )


MANAGE_CONNECTIONS_SCHEMA = {
    "name": "manage_connections",
    "description": (
        "Connect the user to local MCP servers from the bundled catalog. Targets go in "
        "'connectors' as {\"name\": \"linear\", \"mcp\": true}. 'install' adds a catalog entry, "
        "'enable' re-enables a disabled configured server, 'authorize' runs its OAuth; 'status' "
        "lists what is configured. They show the user an approval card and block until it "
        "settles. Never hand-edit mcp_servers config — always use this tool. After a skip or a "
        "timeout, do not re-ask on your own: continue without the server or ask in chat. A later "
        "request from the USER for that same server is not a re-ask — run it. A connected "
        "server's tools are named in the result and are callable at once through "
        "tool_describe/tool_call. Where no card exists an MCP target runs at once and the result "
        "says what happened, with a link for the user to open when one is needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ALL_ACTIONS),
                "description": (
                    "Defaults to status. install, enable and authorize take mcp:true targets."
                ),
            },
            "connectors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "mcp": {
                            "type": "boolean",
                            "description": "Always true: a local MCP server from the catalog.",
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "description": (
                    "Targets. REQUIRED for install, enable and authorize (e.g. "
                    "[{\"name\": \"linear\", \"mcp\": true}]); optional filter for status."
                ),
            },
        },
        "required": [],
    },
}


registry.register(
    name="manage_connections",
    toolset="connections",
    schema=MANAGE_CONNECTIONS_SCHEMA,
    handler=lambda args, **kw: manage_connections(
        args, session_id=kw.get("session_id"), tool_call_id=kw.get("tool_call_id"),
    ),
    emoji="🔌",
)

# Registry dispatch has no card callback, so it answers with the CLI pointer.
registry.register(
    name="manage_catalog",
    toolset="catalog",
    schema=MANAGE_CATALOG_SCHEMA,
    handler=lambda args, **kw: manage_catalog(args, session_id=kw.get("session_id")),
    emoji="🧩",
)
