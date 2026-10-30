"""The opening step of a first-run onboarding chat, for any front end.

The prelude is a script generator the conversation loop plays before the first model call
(``agent/turn_scripted_prelude``): the greeting, then the name card, both as tool rows. It runs only
while the name beat was never shown, and a greeting already in the chat (a relaunch) is not said twice.

Kept out of the gateway so every shell — the desktop app and the avatar window — plays the same
opening from one place.
"""

from __future__ import annotations

from typing import Any


def opening_prelude(agent: Any, history: list):
    """The opening's scripted step, or None when there is nothing to play."""
    from agent.onboarding_flow import PENDING, is_active
    from agent.onboarding_record import load
    from agent.onboarding_text import greeting

    if "setup_beat" not in (getattr(agent, "valid_tool_names", None) or ()):
        return None
    record = load(agent._session_db, agent.session_id)
    if not is_active(record) or record["beats"]["name"] != PENDING:
        return None
    hello = greeting((record.get("facts") or {}).get("locale"))
    said = any(m.get("role") == "assistant" and m.get("content") == hello for m in history)

    def opening():
        yield ("" if said else hello), "setup_beat", {"beat": "name"}

    return opening()
