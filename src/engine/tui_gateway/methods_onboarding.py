import threading
from pathlib import Path

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

# onboarding.state and onboarding.set_run are unscoped on purpose: onboarding.run and the first-chat
# flag live in the root profile's config.yaml whichever profile this backend was launched under.
@method("onboarding.state")
def _(rid, params: dict) -> dict:
    return _ok(rid, _run_state(assume_fresh=bool(params.get("assume_fresh"))))


@method("onboarding.set_run")
def _(rid, params: dict) -> dict:
    from noesia_cli.onboarding_run import set_run
    if not isinstance(params.get("run"), bool):
        return _err(rid, 4002, "onboarding.set_run requires a boolean 'run'")
    try:
        set_run(params["run"], mark_profile_offered=bool(params.get("mark_profile_offered")))
    except Exception as e:
        logger.exception("onboarding.set_run failed")
        return _err(rid, 5077, str(e))
    return _ok(rid, _run_state())


def _run_state(*, assume_fresh: bool = False) -> dict:
    """``run``: the root config says so, else this OS user never used Noesia (or the unpackaged
    desktop's ``--fresh-install`` says to assume that). ``eligible``: this is the desktop's local
    backend and a provider is configured to carry the first chat; remote backends answer at once
    and run onboarding only from Settings. On the local backend the answer comes from the boot
    computation (``onboarding_due``); ``--fresh-install`` asks live."""
    from noesia_cli import onboarding_due
    from noesia_cli.process_identity import on_local_desktop_backend
    from noesia_cli.onboarding_run import should_run

    if not on_local_desktop_backend():
        return {"run": should_run(assume_fresh=assume_fresh), "eligible": False, **_active_run()}
    due = _due_now(assume_fresh=True) if assume_fresh else onboarding_due.read(_due_now, stamp=_due_stamp)
    return {**due, **_active_run()}


def _due_now(*, assume_fresh: bool = False) -> dict[str, bool]:
    from noesia_cli.onboarding_run import should_run

    # Read in the default profile, where onboarding runs, not the launch profile.
    home = _profile_home("default")
    eligible = _provider_configured({"profile_home": str(home) if home else None})
    return {"run": should_run(assume_fresh=assume_fresh), "eligible": eligible}


def _due_stamp() -> tuple:
    """The files the due answer reads, by their config-cache signature: the root config
    (``onboarding.run``), the default profile's config, ``.env`` and ``auth.json`` (what carries a
    first chat) and the managed layer's ``config.yaml`` and ``.env`` (an admin's onboarding or
    provider policy)."""
    from noesia_cli import managed_scope
    from noesia_constants import get_default_noesia_root
    from utils import file_signature

    home = Path(_profile_home("default") or get_default_noesia_root())
    managed = managed_scope.get_managed_dir()
    paths = (get_default_noesia_root() / "config.yaml", home / "config.yaml", home / ".env", home / "auth.json",
             *((managed / "config.yaml", managed / ".env") if managed else ()))
    return tuple(file_signature(st) if (st := _stat(path)) else None for path in paths)


def _stat(path):
    try:
        return path.stat()
    except OSError:
        return None


def start_due_boot() -> None:
    """Serve boot, desktop's local backend: work out the due answer off the RPC path."""
    from agent.memory_provider import spawn_context_thread
    from noesia_cli import onboarding_due
    from noesia_cli.process_identity import on_local_desktop_backend

    if not on_local_desktop_backend():
        return
    finish = onboarding_due.begin_boot(_due_stamp)

    def compute() -> dict[str, bool]:
        return _due_now()

    # finish() marks the boot done even when compute raises, so no reader waits on a dead thread.
    spawn_context_thread(finish, name="onboarding-due", args=(compute,)).start()


# ── first-run onboarding: one chat in the default profile, driven by setup_beat ───────────────

# Onboarding always runs in the default profile, whichever profile this backend was launched under.
_ONBOARDING_PROFILE = {"profile": "default"}
# Two windows pressing start at once must not both find no run and open two onboarding chats.
_onboarding_start_lock = threading.Lock()


def _active_run() -> dict:
    """The onboarding chat in progress and its run id, both None when no run is open."""
    from agent.onboarding_record import active_session_id, load
    with _profile_db(_ONBOARDING_PROFILE) as db:
        session_id = None if db is None else active_session_id(db)
        return {"session_id": session_id, "run_id": (load(db, session_id) or {}).get("run_id") if session_id else None}


class _NoModelForTheRun(Exception):
    """Nothing can carry the run's first chat: no provider is set up in this profile."""


def _provider_configured(scope: dict) -> bool:
    """Something can carry a chat in *scope*'s profile: a key, a config pin, a host credential.
    The provider resolver itself answers."""
    from noesia_cli.auth import resolve_provider
    from noesia_cli.auth_constants import AuthError

    with _session_profile_runtime_scope(scope):
        try:
            if not resolve_provider("auto"):
                return False
        except AuthError:  # no_provider_configured
            return False
        # 有 Key 不等于能聊天：还得真选了一个模型（设置里「启用」过）。没有模型就开场，
        # 引导卡会落在一个连模型都没有的会话上——用户看到的是「还没选模型就 onboard 了」。
        # 这里直接读配置（本函数绑在 server 命名空间上，`_load_cfg`/`os` 都从那儿来）。
        cfg_model = _load_cfg().get("model")
        if isinstance(cfg_model, dict):
            chosen = str(cfg_model.get("default") or "").strip()
        else:
            chosen = str(cfg_model or "").strip()
        return bool(chosen or os.environ.get("NOESIA_MODEL", "").strip())


@method("onboarding.start")
def _(rid, params: dict) -> dict:
    from agent.onboarding_facts import read_facts
    from agent.onboarding_flow import End, Facts
    from agent.onboarding_record import advance, load, open_session_ids, start
    from agent.onboarding_text import CHAT_TITLE, opening_prompt
    # The app's once-per-launch start: closing the app mid-run does not count, so that run's chat goes
    # and the run starts over. A second window or a reconnect sends no flag and gets the run back.
    fresh_launch = bool(params.get("fresh_launch"))
    # Held through the opening submit: two windows pressing start at once get one chat and one opening.
    with _onboarding_start_lock:
        try:
            with _profile_db(_ONBOARDING_PROFILE, writer=True) as db:
                if db is None:
                    return _db_unavailable_error(rid, code=5000)
                open_ids = open_session_ids(db)
                stored = None if params.get("rerun") or fresh_launch else next(iter(open_ids), None)
                created = stored is None
                if created:
                    # The tours setting is the default profile's, whichever profile launched the backend.
                    home = _profile_home("default")
                    scope = {"profile_home": str(home) if home else None}
                    with _session_profile_runtime_scope(scope, hydrate_secrets=False):
                        facts = read_facts(
                            facts_source=params.get("facts_source") or "local",
                            locale=params.get("locale"),
                            app_ui=bool(params.get("app_ui", True)),
                            first_task=bool(params.get("first_task", True)))
                    # A rerun runs on the configured model; a fresh run needs something to run on.
                    if not params.get("rerun") and not _provider_configured(scope):
                        raise _NoModelForTheRun("No provider is set up for the first chat.")
                    stored = db.create_session(new_session_id(), "desktop")
                    # The record exists before the agent is built, so the build folds the onboarding toolset in.
                    start(db, stored, facts)
                else:
                    facts = Facts.from_stored(load(db, stored)["facts"])
                run_id = load(db, stored).get("run_id") or ""
                # One open run at a time. The kept run is open before the others end, so the install
                # flag never closes in between; finishing it closes the flag.
                replaced = [sid for sid in open_ids if sid != stored]
                if fresh_launch:
                    _discard_unfinished_runs(db, replaced)
                else:
                    for old in replaced:
                        advance(db, old, End("replaced"))
                if created:
                    # After the discard, so a run started over keeps the title its deleted chat had.
                    db.set_session_title(stored, db.get_next_title_in_lineage(CHAT_TITLE))
                # A chat with no messages never got its opening (a restart between start and the first turn).
                opening = None if (db.get_session(stored) or {}).get("message_count") else opening_prompt(facts)
        except _NoModelForTheRun as e:
            logger.info("onboarding.start: no chat opened: %s", e)
            return _err(rid, 5080, str(e))
        except Exception as e:
            logger.exception("onboarding.start failed")
            return _err(rid, 5078, str(e))
        _withdraw_setup_cards([] if fresh_launch else replaced)
        resumed = _methods["session.resume"](rid, {**_ONBOARDING_PROFILE, "session_id": stored, "source": "desktop"})
        if "error" in resumed:
            return resumed
        sid = resumed["result"]["session_id"]
        if opening is not None:
            # The opening rules ride a hidden user turn; its prelude plays the greeting and the name card.
            submitted = _methods["prompt.submit"](rid, {"session_id": sid, "text": opening, "display_kind": "hidden"})
            if "error" in submitted:
                return submitted
    return _ok(rid, {"session_id": sid, "stored_session_id": stored, "resumed": not created, "run_id": run_id})


@method("onboarding.skip")
def _(rid, params: dict) -> dict:
    from agent.onboarding_flow import End
    from agent.onboarding_record import advance, load
    session, err = _sess_nowait(params, rid)
    if err:
        return err
    agent = session.get("agent")
    stored = getattr(agent, "session_id", None) or session["session_key"]
    with _session_db(session) as db:
        if load(db, stored) is None:
            return _ok(rid, {"skipped": False})
        step = advance(db, stored, End("skipped"))
    # The card that waits returns to setup_beat, which reads the skipped record.
    _cancel_session_requests(params["session_id"], "resolved", method="setup_beat")
    return _ok(rid, {"skipped": bool(step.result.get("skipped_all"))})


def _live_session_ids(stored_ids: list[str]) -> list[str]:
    """The live sessions here that show one of *stored_ids*."""
    if not stored_ids:
        return []
    with _sessions_lock:
        return [sid for sid, sess in _sessions.items()
                if (getattr(sess.get("agent"), "session_id", None) or sess.get("session_key")) in stored_ids]


def _withdraw_setup_cards(stored_ids: list[str]) -> None:
    """Close the card still waiting in a chat whose run just ended; its answer would be refused."""
    for sid in _live_session_ids(stored_ids):
        _cancel_session_requests(sid, "resolved", method="setup_beat")


def _discard_unfinished_runs(db, stored_ids: list[str]) -> None:
    """A new launch's start: the runs the app was closed on never count as done. Each chat is closed if
    still live here (a backend that outlived the app), then deleted the way ``session.delete`` deletes
    it, which ends its run (``deleted``) and tells every window. A chat the delete refuses keeps its
    messages, and its run ends as ``replaced`` so it is never resumed."""
    from agent.onboarding_flow import End
    from agent.onboarding_record import advance, delete_chat
    for sid in _live_session_ids(stored_ids):
        _close_session_by_id(sid, end_reason="onboarding_restart")
    sessions_dir = Path(_profile_home("default") or get_noesia_home()) / "sessions"
    for old in stored_ids:
        try:
            deleted = delete_chat(db, old, lambda old=old: db.delete_session(
                old, sessions_dir=sessions_dir, exclude_active_write_guards=True))
        except Exception:
            logger.exception("onboarding.start could not delete unfinished chat %s", old)
            deleted = False
        if not deleted:
            advance(db, old, End("replaced"))


def _setup_beat_request(sid: str, payload: dict) -> dict | None:
    from agent.onboarding_flow import CARD_TIMEOUT_S
    from tui_gateway import server_requests
    return server_requests.send("setup_beat", sid, dict(payload), timeout=CARD_TIMEOUT_S)


def _builds_onboarding(agent) -> bool:
    return "setup_beat" in (getattr(agent, "valid_tool_names", None) or ())


def _onboarding_toolsets(enabled, db, session_id: str):
    """*enabled* plus ``onboarding`` for a chat whose run is in progress. Decided once, at agent build:
    the tool array is part of the cached prefix, so a finished run keeps setup_beat (as a refusal)
    until the session is rebuilt."""
    from agent.onboarding_flow import is_active
    from agent.onboarding_record import load
    if _keeps_toolsets(enabled):
        return enabled
    return _with_onboarding(enabled) if is_active(load(db if db is not None else _get_db(), session_id)) else enabled


def _carry_onboarding_toolset(enabled, agent):
    """A tool refresh keeps the onboarding toolset the agent was built with."""
    if _keeps_toolsets(enabled) or "onboarding" not in (getattr(agent, "enabled_toolsets", None) or ()):
        return enabled
    return _with_onboarding(enabled)


def _keeps_toolsets(enabled) -> bool:
    """An explicit empty list is a zero-tool chat; None is unrestricted and still gets the fold-in."""
    return enabled is not None and (not enabled or "onboarding" in enabled)


def _with_onboarding(enabled) -> list[str]:
    """*enabled* plus onboarding. None (every toolset outside the session-gated ones, which onboarding
    is) is spelled out by name: the agent's other toolset gates read the names, as they read None."""
    if enabled is None:
        from toolsets import TOOLSET_SESSION_PLATFORMS, get_all_toolsets
        enabled = [name for name in get_all_toolsets() if name not in TOOLSET_SESSION_PLATFORMS]
    return [*enabled, "onboarding"]


def _onboarding_prelude(agent, history: list):
    """The opening's scripted step (agent/onboarding_turn.py): one implementation for every shell."""
    from agent.onboarding_turn import opening_prelude
    return opening_prelude(agent, history)


def _stage_onboarding_turn_note(sid: str, agent, history_empty: bool) -> None:
    """Before a user turn in the onboarding chat, while no card waits: which card is closed and how to
    bring it back. Rides the turn's API-only notes, like the first-contact note."""
    from agent.onboarding_flow import is_active
    from agent.onboarding_record import load
    from agent.onboarding_text import turn_note
    from tui_gateway import server_requests
    if history_empty or not _builds_onboarding(agent) or server_requests.pending_kind(sid) == "setup_beat":
        return
    record = load(agent._session_db, agent.session_id)
    if is_active(record) and (note := turn_note(record)):
        prior = getattr(agent, "_gateway_turn_context_notes", "") or ""
        agent._gateway_turn_context_notes = f"{prior}\n\n{note}" if prior else note


def register(server) -> None:
    from agent.onboarding_record import on_ended

    bind_module(globals(), server, skip=("_",))
    # Every window, not one session: the window showing the run may be subscribed to another chat.
    on_ended(lambda ended: server._broadcast_global_event("onboarding.ended", ended))
