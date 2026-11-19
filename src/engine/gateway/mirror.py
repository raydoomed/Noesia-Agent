"""Session mirroring for cross-platform message delivery.

When a message is sent to a platform (send_message or cron delivery), append a
"delivery-mirror" record to the target session's transcript so the receiving-side
agent knows what was sent.  Standalone: works from CLI, cron and gateway contexts.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from noesia_cli.config import get_noesia_home

logger = logging.getLogger(__name__)

_SESSIONS_DIR = get_noesia_home() / "sessions"
_SESSIONS_INDEX = _SESSIONS_DIR / "sessions.json"
_SESSIONS_INDEX_AT_IMPORT = _SESSIONS_INDEX


def _resolve_sessions_index() -> Path:
    """Active profile's ``sessions.json`` at call time: the patched ``_SESSIONS_INDEX`` when a test
    changed it, else live profile-scoped NOESIA_HOME — under the multiplexed gateway one process
    serves every profile, so the import-time constant would resolve every profile's pre-migration
    session lookup against the launch profile's index."""
    return (_SESSIONS_INDEX if _SESSIONS_INDEX != _SESSIONS_INDEX_AT_IMPORT
            else get_noesia_home() / "sessions" / "sessions.json")


def _origin_user_id(entry: dict) -> str:
    return str((entry.get("origin") or {}).get("user_id") or "")


def mirror_to_session(
    platform: str, chat_id: str, message_text: str, source_label: str = "cli", thread_id: Optional[str] = None,
    user_id: Optional[str] = None, role: str = "assistant", session_id: Optional[str] = None,
    notice: Optional[dict] = None,
) -> bool:
    """Append a delivery-mirror message to the target session's SQLite transcript.

    Pass ``session_id`` when the caller already holds the exact session (e.g. the
    cron in_channel seed) to skip the origin scan, which refuses to guess on a
    populated chat (flat + N thread sessions per chat_id) and would drop the mirror.
    Text that is NOT the agent speaking (e.g. a cron brief) must pass
    ``role="user"``: ``mirror`` metadata is dropped at the SQLite boundary, so an
    assistant-role mirror replays as a real turn and yields assistant→assistant
    pairs that break strict-alternation providers, while a user-role mirror
    collapses safely via the consecutive-user merge.
    Returns True if mirrored, False if no matching session or error. Never raises.

    ``role`` defaults to ``"assistant"`` — correct for the interactive ``send_message`` mirror, where the
    mirrored text is the agent's own outgoing reply (a genuine assistant turn). See #2221.

    ``notice`` rides along on the ``session.appended`` broadcast (job id + name for cron) so an open
    desktop can slide down a notification card; it is not written to the transcript.
    """
    try:
        if not session_id:
            session_id = _find_session_id(platform, str(chat_id), thread_id=thread_id, user_id=user_id)
        if not session_id:
            logger.warning(
                "Mirror: no session found for %s:%s thread=%s user=%s (explicit_id=none, origin-scan bailed)",
                platform, chat_id, thread_id, user_id,
            )
            return False
        _append_to_sqlite(session_id, {
            "role": role, "content": message_text, "timestamp": datetime.now().isoformat(),
            "mirror": True, "mirror_source": source_label,
        })
        _emit_session_appended(session_id, notice)
        logger.debug("Mirror: wrote to session %s (from %s)", session_id, source_label)
        return True
    except Exception as e:
        # WARNING, not debug: a silent mirror drop is the cron continuation-amnesia bug.
        logger.warning("Mirror failed for %s:%s thread=%s user=%s session=%s: %s", platform, chat_id, thread_id, user_id, session_id, e)
        return False


def _find_session_id(platform: str, chat_id: str, thread_id: Optional[str] = None, user_id: Optional[str] = None) -> Optional[str]:
    """Active session_id for a platform + chat_id pair.

    state.db is primary; sessions.json is the pre-migration fallback.  DM keys
    don't embed the chat_id ("agent:main:telegram:dm"), so match on the persisted
    origin.  With *user_id*, exact sender matches win; several same-chat candidates
    with no user match → None rather than contaminate another participant's session.

    Queries state.db gateway session rows (primary source since #9006); falls back to scanning sessions.json
    for pre-migration databases.
    """
    try:
        from noesia_state_registry import acquire, release_or_close
        db = acquire()
        try:
            finder = getattr(db, "find_session_by_origin", None)
            session_id = finder(platform=platform, chat_id=chat_id, thread_id=thread_id, user_id=user_id) if callable(finder) else None
            if session_id:
                return str(session_id)
        finally:
            release_or_close(db)
    except Exception as e:
        logger.debug("Mirror state.db session lookup failed: %s", e)

    sessions_index = _resolve_sessions_index()
    if not sessions_index.exists():
        return None
    try:
        data = json.loads(sessions_index.read_text(encoding="utf-8-sig"))
    except Exception:
        return None

    def _matches(entry: dict) -> bool:
        origin = entry.get("origin") or {}
        return ((origin.get("platform") or entry.get("platform", "")).lower() == platform.lower()
                and str(origin.get("chat_id", "")) == str(chat_id)
                and (thread_id is None or str(origin.get("thread_id") or "") == str(thread_id)))

    # Keys starting with "_" (e.g. the gateway's "_README") are metadata sentinels.
    candidates = [e for k, e in data.items() if not str(k).startswith("_") and isinstance(e, dict) and _matches(e)]
    if not candidates:
        return None
    if user_id:
        exact_user_matches = [e for e in candidates if _origin_user_id(e) == str(user_id)]
        if exact_user_matches:
            candidates = exact_user_matches
        elif len(candidates) > 1:
            return None
    elif len(candidates) > 1 and len({u.strip() for u in map(_origin_user_id, candidates) if u.strip()}) > 1:
        return None
    return max(candidates, key=lambda entry: entry.get("updated_at", "")).get("session_id")


def newest_conversation(platform: str, user_id: str = "") -> Optional[dict]:
    """这个平台上最近还在用的那场对话（``deliver=latest`` 用）。

    排序和界面同一个来源：state.db 的会话列表，按链尾最近活动倒序。跳过 cron 自己的运行
    会话——那是每次运行新开的临时壳，投进去没人看得到。刚点开、还没说话的新会话也算：它的
    最近活动就是被创建的时刻。给 ``user_id`` 时只认同一个人，多用户机器上不会把别人的会话当
    成投递目标。
    """
    try:
        from noesia_state_registry import acquire, release_or_close
        db = acquire()
        try:
            rows = db.list_sessions_rich(
                order_by_last_active=True, limit=50,
                exclude_sources=["cron", "subagent"])
        finally:
            release_or_close(db)
    except Exception as e:
        logger.debug("newest_conversation: session list failed: %s", e)
        return None
    for row in rows or []:
        try:
            origin = json.loads(row.get("origin_json") or "{}")
        except Exception:
            origin = {}
        row_platform = str(origin.get("platform") or "").strip().lower()
        if row_platform != str(platform or "").strip().lower():
            continue
        chat_id = str(origin.get("chat_id") or row.get("chat_id") or "")
        if not chat_id:
            continue
        row_user = str(row.get("user_id") or "")
        if user_id and row_user and row_user != str(user_id):
            continue
        return {"platform": row_platform, "chat_id": chat_id, "user_id": row_user}
    return None


def _emit_session_appended(session_id: str, notice: Optional[dict] = None) -> None:
    """有人往这场会话里直接写了一行：广播一声，开着的桌面滑下一张通知卡。

    走全局广播而不是 `_emit`：桌面连的是 WS，只在跑这一轮时挂着「当前传输」，
    会话缓存的 transport 在后台线程里查不到，`_emit` 会掉到 stdio（写进
    gateway-stdio.log，界面收不到）。广播出去由界面按 payload 里的 session_id 认自己那场。
    """
    try:
        from tui_gateway.server import _broadcast_global_event
        _broadcast_global_event("session.appended", {"session_id": session_id, **(notice or {})})
    except Exception as e:
        logger.debug("session.appended 推送失败 %s: %s", session_id, e)


def _append_to_sqlite(session_id: str, message: dict) -> None:
    """Append a message to the SQLite session database.

    Raises on failure: ``mirror_to_session`` reports ``False`` (and warns) only when the
    exception reaches it — swallowing it here made every failed write look mirrored (#10130).
    """
    from noesia_state_registry import acquire, release_or_close

    db = acquire()
    try:
        db.append_message(session_id=session_id, role=message.get("role", "assistant"), content=message.get("content"))
    finally:
        release_or_close(db)
