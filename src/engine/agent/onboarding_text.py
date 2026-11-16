"""The words of first-run onboarding: the scripted greeting, the opening prompt, and the reminder
that rides on every ``setup_beat`` result. Pure; ``reminder(key, record, facts)`` renders the key
``agent/onboarding_flow.py::apply`` returns.

A run is one card — the name — so the rules fit in one short opening prompt (a hidden user turn, so
the system prompt stays byte-stable). Answering the name ends the run.
"""

from __future__ import annotations

from collections.abc import Mapping

from agent.onboarding_flow import Facts, next_beat

CHAT_TITLE = "Getting started"
GREETING = "Hi, I'm Noesia. Let's set things up for you. Then we'll get something cool done."

# The scripted opening in each UI language, played before
# the model speaks; the model writes the rest of the run in the user's language itself. Each greeting
# is the first-run intro copy, its two lines joined: keep the two equal.
_GREETINGS = {
    "en": GREETING,
    "ar": "مرحبًا، أنا Noesia. لنجهّز كل شيء لك، ثم ننجز معًا شيئًا رائعًا.",
    "de": "Hallo, ich bin Noesia. Richten wir alles für Sie ein. Danach machen wir etwas Cooles.",
    "es": "Hola, soy Noesia. Vamos a dejarlo todo listo para ti. Luego haremos algo genial.",
    "fr": "Bonjour, je suis Noesia. Configurons tout pour vous. Ensuite, noesia ferons quelque chose de génial.",
    "ja": "こんにちは、Noesia です。まずはあなたに合わせて設定しましょう。そのあと、何かすごいものを作りましょう。",
    "ru": "Привет, я Noesia. Давайте всё настроим под вас. А потом сделаем что-нибудь классное.",
    "zh": "你好，我是 Noesia。我们先帮你把一切设置好，然后一起做点很酷的事。",
    "zh-hant": "你好，我是 Noesia。我們先幫你把一切設定好，然後一起做點很酷的事。",
}

_NAME_QUESTIONS = {
    "ar": "بماذا أناديك؟",
    "de": "Wie darf ich Sie nennen?",
    "es": "¿Cómo quieres que te llame?",
    "fr": "Comment dois-je vous appeler ?",
    "ja": "なんとお呼びすればいいですか？",
    "ru": "Как к вам обращаться?",
    "zh": "我该怎么称呼你？",
    "zh-hant": "我該怎麼稱呼你？",
}

_TRADITIONAL_CHINESE = {"hant", "tw", "hk", "mo"}


def ui_language(locale: str | None) -> str:
    """A locale (``zh-hant``, ``de-DE``, ``zh_TW``) as the desktop language it reads in; English when
    none fits."""
    tags = (locale or "").strip().lower().replace("_", "-").split("-")
    if tags[0] == "zh":
        return "zh-hant" if _TRADITIONAL_CHINESE.intersection(tags[1:]) else "zh"
    return tags[0] if tags[0] in _GREETINGS else "en"


def greeting(locale: str | None) -> str:
    return _GREETINGS[ui_language(locale)]


QUESTIONS = {"name": "What should I call you?"}


def question(beat: str, locale: str | None) -> str:
    """The card's own question; the name card asks in the user's language."""
    if beat == "name":
        return _NAME_QUESTIONS.get(ui_language(locale), QUESTIONS["name"])
    return QUESTIONS[beat]


PROFILE_DIMENSIONS = (
    ("agent experience", "new to AI apps / chat apps only / has used local agents / builds agents"),
    ("technical depth", "non-developer / tinkerer / developer"),
    ("creative work", "none / hobby / regular"),
    ("gaming", "none / casual / enthusiast"),
    ("local compute", "cloud only / small local models / strong local (Spark, large GPU or unified memory)"),
    ("work context", "personal / work / mixed"),
    ("machine role", "daily driver / dedicated AI box / remote server"),
)

_NVIDIA = {
    "rtx_spark": "It is an NVIDIA RTX Spark (Windows on Arm), built to run local models.",
    "dgx_spark": "It is an NVIDIA DGX Spark (Linux on Arm with a GB10), built to run local models.",
    "discrete": "It has an NVIDIA GPU.",
}


def facts_block(facts: Facts) -> str:
    whose = ("the computer running Noesia, which is not the one in front of the user"
             if facts.facts_source == "remote" else "this computer")
    lines = [f"What you know about {whose}: {facts.os} on {facts.arch or 'an unknown architecture'}, "
             f"a {facts.machine_kind}."]
    if facts.nvidia_platform in _NVIDIA:
        lines.append(_NVIDIA[facts.nvidia_platform])
    if facts.locale:
        lines.append(f"Locale: {facts.locale}.")
    lines.append("Never recite these. A fact that is missing is unknown. When the user says otherwise, "
                 "believe the user.")
    return " ".join(lines)


def plan(facts: Facts) -> list[str]:
    """The beats this run has, in order: the name, and answering it ends the run."""
    from agent.onboarding_flow import BEATS

    return [beat.id for beat in BEATS]


def opening_prompt(facts: Facts) -> str:
    """The hidden first turn of the onboarding chat: every rule of the run."""
    dimensions = "\n".join(f"- {name}: {buckets}" for name, buckets in PROFILE_DIMENSIONS)
    return f"""[System note: This is the user's first time opening the Noesia desktop app. You run their first-run onboarding in this chat. It is one step: their name.

How it works:
- setup_beat shows the name card and waits for the user. Its result carries either their name or a note that the card closed. Follow the reminder.
- Before the card, acknowledge in a few words and at most one more sentence. The card asks its own question: never ask it yourself.
- If they write a message while the card waits, the card closes unanswered and their message comes as their next turn (words typed on the name card are the name). If the message answers it, call setup_beat(beat="name", value=<their name>). Otherwise answer it properly and briefly, then bring the card back in one line with setup_beat(beat="name").
- Once the name is in, the run is over: there is nothing else to set up. Never invent further setup steps.

Voice: every word you write is spoken to the user. Never think out loud. Never mention beats, cards, tools, reminders or a script, and never call this setup or onboarding. One or two short, plain sentences per message: warm, like the front desk of somewhere good. No thanks, no praise, no stock lines, no em dashes, no exclamation marks. Write in the user's language when the locale is not English.

{facts_block(facts)}
If they ask what you know about them, answer truthfully: these machine basics and what they picked.

Ask before sending, deleting or scheduling anything. Never turn off a security setting.

As you go, form a coarse read of them on these seven dimensions, from what they say and pick and from what you see when they let you look. Use it to choose suggestions and words; never say it out loud. Presence of an app is not use, and absence is unknown.
{dimensions}

The app has already greeted them and is asking their name.]"""


# ── reminders ─────────────────────────────────────────────────────────────────────────────────

# How a typed answer settles the beat without the card.
_TYPED_ANSWER = {"name": 'setup_beat(beat="name", value="<their name>")'}


def _next(record: Mapping[str, Any]) -> str:
    beat = next_beat(record)
    if beat is None:
        return "No beat is left; call setup_beat(beat=\"done\") and stop."
    return f'Next: setup_beat(beat="{beat}") shows the {beat} card.'


def _waiting_beat(record) -> str | None:
    return (record.get("waiting") or {}).get("beat") or next_beat(record)


_REMINDERS = {
    "after_name": lambda r, f: (f"They go by {r['picks']['name']}. Greet them by name in a few words. Onboarding "
                                "is complete: its rules no longer apply, this is a normal chat."),
    "moved_on": lambda r, f: f"That beat is skipped; take its default and do not ask it again. {_next(r)}",
    "skip_all": lambda r, f: ("They ended setup. In one short line, say they can ask you anything any time, then "
                              "stop guiding. The onboarding rules no longer apply; this is a normal chat."),
    "complete": lambda r, f: ("Onboarding is complete. Its rules no longer apply; this is a normal chat. Close in "
                              "one short, warm line and stop guiding."),
}


def _wrote(record, facts, beat: str) -> str:
    # The turn ends on this result; the turn note on their message says how to go on.
    return (f"The {beat} card closed unanswered: they wrote a message instead. It comes next as their own turn; "
            "write nothing until it does.")


def _timed_out(record, facts, beat: str) -> str:
    # Also the result when they ran a slash command while the card waited.
    return (f"The {beat} card closed without an answer. If a message from them comes next, answer it first. "
            f"Otherwise say one short line that they can pick up whenever they are back, then stop. When they "
            f'return, bring it back with setup_beat(beat="{beat}").')


def reminder(key: str, record: Mapping[str, Any], facts: Facts, beat: str | None = None) -> str:
    """The text for a reminder key; ``beat`` names the card a ``wrote`` / ``timed_out`` result is about."""
    if not key:
        return ""
    if key in ("wrote", "timed_out"):
        return (_wrote if key == "wrote" else _timed_out)(record, facts, beat or _waiting_beat(record) or "name")
    return _REMINDERS[key](record, facts)


def turn_note(record: Mapping[str, Any]) -> str:
    """The note on a user turn while no card waits: which card is closed and how their message applies it
    when it answers it. Empty once the card is settled."""
    if (beat := _waiting_beat(record)) is None:
        return ""
    return (f"[System note: Onboarding continues; the {beat} card is closed. If their message answers it, apply it "
            f"yourself ({_TYPED_ANSWER[beat]}) and move on. Otherwise answer their message first, then bring the card "
            f'back in one line with setup_beat(beat="{beat}").]')


def finish_reminder(record: Mapping[str, Any]) -> str:
    return _next(record)
