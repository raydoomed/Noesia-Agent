"""The user's name on their profile: the one ``Name: 「…」`` line in ``memories/USER.md``.

Two writers put it there — the onboarding name card and Settings → Memory — so reading it back has to
accept every shape either of them leaves (a list item, a bold label, ``名字`` / ``姓名`` / ``称呼``),
and a name mentioned in prose is not a name line. These are the same functions the desktop runs
(``app/user_name.py``): the window shows the name that file resolves, so onboarding has to resolve it
the same way or it would ask for a name the window already has.
"""

from __future__ import annotations

import re

#: ``Name: 「…」`` at the start of a line, optionally a list item and/or bold label.
NAME_LINE = re.compile(
    r"^[ \t]*(?:[-*·][ \t]*)?(?:\*\*)?[ \t]*(?:name|名字|姓名|称呼)[ \t]*(?:\*\*)?[ \t]*[:：][ \t]*"
    r"(?:\*\*[ \t]*)?(.+?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

#: Quotes a hand-edited name may wear.
_BRACKETS = "「」『』《》\"'“”‘’"

#: A template placeholder is not a name: ``_(pick something)_``.
_PLACEHOLDER = re.compile(r"^_\(.*\)_$")


def clean_name(value: str) -> str:
    """A name as it is written into the line: no outer quotes, no line breaks, no entry delimiter."""
    return " ".join(value.strip().strip(_BRACKETS).replace("§", " ").split())


def format_name(value: str) -> str:
    """The line a name is written as: ``Name: 「…」``."""
    return f"Name: 「{clean_name(value)}」"


def is_name_entry(entry: str) -> bool:
    """True when the whole entry is a name line — prose that mentions a name is not one."""
    found = NAME_LINE.match(entry)
    return bool(found) and found.end() == len(entry)


def profile_name(text: str) -> str:
    """The name a profile text carries; the last name line wins, as it does on the window."""
    name = ""
    for found in NAME_LINE.finditer(text):
        value = clean_name(found.group(1))
        if not value or len(value) > 40 or _PLACEHOLDER.match(value):
            continue
        name = value
    return name


def saved_name() -> str:
    """The name the profile carries now, read from disk: Settings writes the same file, so a name
    typed there before onboarding started is already visible here."""
    from tools.memory_tool import get_memory_dir

    try:
        text = (get_memory_dir() / "USER.md").read_text(encoding="utf-8-sig")
    except Exception:  # noqa: BLE001 - a profile we cannot read is a profile with no name yet
        return ""
    return profile_name(text)


def write_name(store, name: str) -> None:
    """Put *name* on the profile as its one name line: add it when there is none, otherwise replace the
    first name entry and drop the rest. Entries that only mention a name are left alone, and nothing is
    written when the entry already says it."""
    cleaned = clean_name(name)
    if not cleaned:
        return
    line = format_name(cleaned)
    named = [str(entry) for entry in (getattr(store, "user_entries", []) or []) if is_name_entry(str(entry))]
    if not named:
        store.add("user", line)
        return
    if len(named) == 1 and named[0] == line:
        return
    store.replace("user", named[0], line)
    for stale in named[1:]:
        store.remove("user", stale)
