"""一 session 一份 JSON 转写：每轮结束把整段对话写回同一个文件。

`state.db` 仍是唯一真源，这份文件是同一份对话的可读快照——一个会话永远只有一个文件，
每轮覆盖它，不做「每请求一份」那种排查用的转储（那是 `request_dump_*`，另一回事）。

默认开：新装也一样，用户打开 `sessions/` 就能看到自己的聊天记录。关掉：
`NOESIA_SESSION_TRANSCRIPT=0`。写失败只记日志，绝不打断对话。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from utils import atomic_json_write, env_var_enabled_by_default

logger = logging.getLogger(__name__)


def transcript_path(agent: Any) -> Optional[Path]:
    """This session's transcript file: ``<agent.logs_dir>/<session_id>.json``."""
    session_id = getattr(agent, "session_id", None)
    logs_dir = getattr(agent, "logs_dir", None)
    if not session_id or logs_dir is None:
        return None
    from agent.session_persistence import _safe_session_filename_component

    return Path(logs_dir) / f"{_safe_session_filename_component(session_id)}.json"


def save_session_transcript(agent: Any) -> Optional[Path]:
    """Rewrite this session's JSON transcript from the database; ``None`` when skipped.

    Fail-open by design: a transcript that cannot be written is a cosmetic loss, while raising
    here would kill the turn that just finished.
    """
    if not env_var_enabled_by_default("NOESIA_SESSION_TRANSCRIPT"):
        return None
    target = transcript_path(agent)
    if target is None:
        return None
    try:
        from noesia_state import SessionDB

        # include_compacted / include_inactive: the file should hold everything that was said,
        # not just the window the model still sees after compaction.
        data = SessionDB().export_session(
            str(agent.session_id), include_compacted=True, include_inactive=True
        )
        if not data:
            return None
        data["exported_at"] = datetime.now().isoformat(timespec="seconds")
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(target, data, default=str)
        return target
    except Exception:
        logger.warning(
            "Session transcript write failed (session=%s)", getattr(agent, "session_id", "-"), exc_info=True
        )
        return None
