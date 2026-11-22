"""Point at something in the Noesia desktop GUI and say one line about it — the quiet
sibling of ``tour`` (same ``data-tour`` handles) with no scrim/spotlight/paging.
Fire-and-forget: a tip is not a question, so blocking on a round-trip would stall the
reply. Lives in ``desktop_ui`` but is not exposed to the model: the shell has no renderer for
``tip.show`` yet, so offering the call would only produce failures. Putting it back means
deleting ``check_fn=lambda: False`` *and* restoring the Settings → Appearance switch that used
to gate it (``desktop_ui.user_enabled("in_app_tips", default=True)``) — delete only the check
and tips fire even with the switch off."""

import json

from tools import desktop_ui
from tools.registry import registry, tool_error

SIDES = ("top", "right", "bottom", "left")


def tip_tool(text: str, selector: str, title: str = "", side: str = "") -> str:
    """Show one tip bubble anchored to ``selector``."""
    text = (text or "").strip()
    selector = (selector or "").strip()
    if not text:
        return tool_error("tip needs text — the one line the bubble says.")
    if not selector:
        return tool_error("tip needs a selector to point at. Call tour(action='targets') to see "
                          "what's on screen and prefer a target reporting stable: true.")
    if side and side not in SIDES:
        return tool_error(f"side must be one of: {', '.join(SIDES)}.")
    payload = {"selector": selector, "text": text,
               **{k: v for k, v in (("title", title), ("side", side)) if v}}
    try:
        ok = desktop_ui.emit("tip.show", payload)
    except Exception as exc:
        return tool_error(f"Failed to show the tip: {exc}")
    if not ok:
        return tool_error("tip is only available in the Noesia desktop app.")
    return json.dumps({"success": True, "selector": selector}, ensure_ascii=False)


TIP_SCHEMA = {
    "name": "show_tip",
    "description": (
        "Point at one thing in the desktop UI with a small arrow bubble (no "
        "dimming, no tour chrome) — for when a sentence is clearer with a "
        "finger on its subject. Get selectors from tour(action='targets'), "
        "prefer stable:true, never guess. One tip at a time (new replaces "
        "last); say the same thing in chat too — the bubble is a pointer, "
        "not the message. Sparingly: a bubble every turn stops being read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The one-sentence bubble text.",
            },
            "selector": {
                "type": "string",
                "description": "Selector from tour targets.",
            },
            "title": {
                "type": "string",
                "description": "Optional heading.",
            },
            "side": {
                "type": "string",
                "enum": list(SIDES),
                "description": "Omit for 'top'; flips at screen edges.",
            },
        },
        "required": ["text", "selector"],
    },
}


registry.register(
    name="show_tip", toolset="desktop_ui", schema=TIP_SCHEMA, check_fn=lambda: False,
    handler=lambda args, **kw: tip_tool(
        **{k: args.get(k, "") for k in ("text", "selector", "title", "side")}),
    emoji="💡")
