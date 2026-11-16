"""First-run onboarding as data: the beats, the run record, and how each event moves the record.

Pure: no I/O, no clock. ``apply(record, event, facts, now=...)`` returns the next record, the result
the model reads, and the key of the reminder that rides on it (``agent/onboarding_text.py`` renders
it). The record is stored by ``agent/onboarding_record.py``; ``tools/setup_beat_tool.py`` drives it.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

PENDING, SHOWN, DONE, SKIPPED = "pending", "shown", "done", "skipped"
ONBOARDING, COMPLETE, PHASE_SKIPPED = "onboarding", "complete", "skipped"
CARD_TIMEOUT_S = 300


@dataclass(frozen=True)
class Facts:
    """The safe facts (class A) one run is built from. ``full_name`` reaches only the name card; it is
    never stored in the record or put in a result."""

    os: str  # macos | windows | linux
    arch: str = ""
    machine_kind: str = "computer"  # Mac | PC | Spark | computer
    nvidia_platform: str | None = None  # rtx_spark | dgx_spark | discrete
    facts_source: str = "local"  # local | remote: whose computer these facts describe
    locale: str | None = None
    full_name: str | None = field(default=None, compare=False)

    def stored(self) -> dict:
        data = asdict(self)
        data.pop("full_name")
        return data

    @classmethod
    def from_stored(cls, data: Mapping[str, Any]) -> Facts:
        known = {key: data[key] for key in cls.__dataclass_fields__ if key in data and key != "full_name"}
        return cls(**known)


# ── events ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Shown:
    """A card is on screen, waiting for the user."""

    beat: str
    tool_call_id: str = ""


@dataclass(frozen=True)
class Answered:
    """``said``: the answer came as words in the composer (the name card), so the chat shows them as
    the user's bubble."""

    beat: str
    answer: Mapping[str, Any]
    said: str = ""


@dataclass(frozen=True)
class Wrote:
    """The user sent a message instead of answering the card. The message is their own next turn, so the
    result carries none of it."""

    beat: str


@dataclass(frozen=True)
class TimedOut:
    beat: str


@dataclass(frozen=True)
class Recorded:
    beat: str
    value: str


@dataclass(frozen=True)
class Skipped:
    beat: str


@dataclass(frozen=True)
class End:
    """The run ended outside the model: ``skipped`` (Skip setup), ``replaced`` (a rerun opened a new
    run) or ``deleted`` (its chat is being deleted)."""

    outcome: str = "skipped"


@dataclass(frozen=True)
class Done:
    pass


Event = Shown | Answered | Wrote | TimedOut | Recorded | Skipped | End | Done


@dataclass(frozen=True)
class Step:
    record: dict
    result: dict
    reminder: str = ""


# ── beats ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Beat:
    """``card`` is the beat's data for the desktop; ``picks`` are the answer keys recorded; ``value``
    is the pick a ``value`` given in words sets (None: the beat takes no value)."""

    id: str
    card: Callable[[Facts, Mapping], dict]
    picks: tuple[str, ...]
    value: str | None = None

    def after(self, picks: Mapping[str, Any]) -> str:
        """Reminder key for the result that settles this beat."""
        return f"after_{self.id}"


BEATS: tuple[Beat, ...] = (
    Beat("name", lambda facts, picks: {}, picks=("name",), value="name"),
)
BEAT_BY_ID = {beat.id: beat for beat in BEATS}
BEAT_IDS = tuple(BEAT_BY_ID)


def new_record(facts: Facts, *, now: float = 0.0, run_id: str = "") -> dict:
    return {
        "run_id": run_id, "phase": ONBOARDING, "beats": {beat.id: PENDING for beat in BEATS}, "waiting": None,
        "picks": {"name": None},
        "facts": facts.stored(), "ended_by": None,
        "started_at": now, "updated_at": now,
    }


def next_beat(record: Mapping[str, Any]) -> str | None:
    """The first beat still pending or shown; None when every beat is settled."""
    return next((beat for beat in BEAT_IDS if record["beats"][beat] in (PENDING, SHOWN)), None)


def is_active(record: Mapping[str, Any] | None) -> bool:
    return bool(record) and record.get("phase") == ONBOARDING


# ── apply ─────────────────────────────────────────────────────────────────────────────────────


def _next_field(record: Mapping[str, Any]) -> str:
    return next_beat(record) or "done"


def _settle(record: dict, beat: str, patch: Mapping[str, Any]) -> None:
    record["beats"][beat] = DONE
    record["picks"].update({key: value for key, value in patch.items() if key in BEAT_BY_ID[beat].picks})


def _shown(record: dict, event: Shown, facts: Facts, now: float) -> Step:
    if record["beats"][event.beat] != DONE:
        record["beats"][event.beat] = SHOWN
    record["waiting"] = {"beat": event.beat, "tool_call_id": event.tool_call_id, "since": now}
    return Step(record, {})


def valid_answer(beat: str, answer: Mapping[str, Any]) -> bool:
    """A card answer carries only its beat's pick keys. ``label`` is the text the card showed, kept
    for the transcript and never recorded."""
    spec = BEAT_BY_ID[beat]
    if not isinstance(answer.get("label", ""), str):
        return False
    answer = {key: value for key, value in answer.items() if key != "label"}
    if not answer or not set(answer) <= set(spec.picks):
        return False
    return isinstance(answer.get("name"), str) and bool(answer["name"].strip())


def _answered(record: dict, event: Answered, facts: Facts, now: float) -> Step:
    record["waiting"] = None
    if not valid_answer(event.beat, event.answer):
        return Step(record, {"beat": event.beat, "error": "The card sent an answer this beat does not accept. "
                             f'Bring it back with setup_beat(beat="{event.beat}").'})
    _settle(record, event.beat, event.answer)
    said = {"said": event.said} if event.said else {}
    return Step(record, {"beat": event.beat, "answer": dict(event.answer), **said, "next": _next_field(record)},
                BEAT_BY_ID[event.beat].after(record["picks"]))


def _wrote(record: dict, event: Wrote, facts: Facts, now: float) -> Step:
    record["waiting"] = None
    return Step(record, {"beat": event.beat, "wrote_instead": True}, "wrote")


def _timed_out(record: dict, event: TimedOut, facts: Facts, now: float) -> Step:
    record["waiting"] = None
    return Step(record, {"beat": event.beat, "timed_out": True}, "timed_out")


def _recorded(record: dict, event: Recorded, facts: Facts, now: float) -> Step:
    beat = BEAT_BY_ID[event.beat]
    if not isinstance(event.value, str) or not event.value.strip():
        return Step(record, {"error": f"value for {event.beat} must be the words they used.",
                             "next": _next_field(record)})
    _settle(record, event.beat, {beat.value: event.value.strip()} if beat.value else {})
    return Step(record, {"recorded": event.beat, "next": _next_field(record)}, beat.after(record["picks"]))


def _skipped(record: dict, event: Skipped, facts: Facts, now: float) -> Step:
    record["beats"][event.beat] = SKIPPED
    return Step(record, {"recorded": event.beat, "skipped": True, "next": _next_field(record)}, "moved_on")


def _end(record: dict, event: End, facts: Facts, now: float) -> Step:
    record.update(phase=PHASE_SKIPPED, ended_by=event.outcome, waiting=None)
    for beat in BEAT_IDS:
        if record["beats"][beat] != DONE:
            record["beats"][beat] = SKIPPED
    return Step(record, {"skipped_all": True}, "skip_all")


def _done(record: dict, event: Done, facts: Facts, now: float) -> Step:
    return Step(record, {}, "complete")


_HANDLERS: dict[type, Callable[[dict, Any, Facts, float], Step]] = {
    Shown: _shown, Answered: _answered, Wrote: _wrote, TimedOut: _timed_out, Recorded: _recorded,
    Skipped: _skipped, End: _end, Done: _done,
}
_ENDED = "Onboarding is over in this chat; it is a normal chat now."


def _ended(record: dict, event: Event) -> Step:
    """An ended run refuses setup_beat, except a repeat of its own end: done on a completed run and
    any end on an ended run answer as if they had ended it."""
    if isinstance(event, Done) and record.get("phase") == COMPLETE:
        return Step(record, {"completed": True}, "complete")
    if isinstance(event, End):
        return Step(record, {"skipped_all": True})
    return Step(record, {"error": _ENDED})


def apply(record: Mapping[str, Any], event: Event, facts: Facts, *, now: float = 0.0) -> Step:
    """The record after ``event``, the model-facing result, and the reminder key. The run completes when
    no beat is left pending or shown: the name card's answer settles the last one."""
    working = copy.deepcopy(dict(record))
    if not is_active(working):
        return _ended(working, event)
    step = _HANDLERS[type(event)](working, event, facts, now)
    if is_active(step.record) and next_beat(step.record) is None:
        step.record.update(phase=COMPLETE, ended_by="done", waiting=None)
        step = Step(step.record, {**step.result, "completed": True}, step.reminder)
    if step.record != record:
        step.record["updated_at"] = now
    return step
