"""档位在「界面显示」这一侧需要的两个数：选的那档、这条路由实际会发的那档。

路由的档位表由引擎自己算（`gateway.session_local.effort_fields` → `clamp_effort` +
`route_supported_efforts`），界面不写死任何模型的档位，所以换模型/换 provider 都不用改界面。
"""

from __future__ import annotations


def reasoning_wire_now(session: dict | None, cfg: dict | None, reasoning_config, effort: str) -> str:
    """``effort`` 这条路由实际会发出去的那档；没降档时与它相同，算不出来时回空串。"""
    if not isinstance(reasoning_config, dict) or str(effort or "") in {"", "none"}:
        return ""
    from gateway.session_local import effort_fields

    model_cfg = (cfg or {}).get("model") if isinstance((cfg or {}).get("model"), dict) else {}
    model, provider = model_cfg.get("default") or "", model_cfg.get("provider") or ""
    base_url, api_mode = model_cfg.get("base_url") or "", model_cfg.get("api_mode")
    session = session or {}
    if session.get("agent") is not None:  # 会话建好 agent 后以它为准
        from agent.voice_turn_route import session_runtime_view

        view = session_runtime_view(session.get("agent"))
        model = getattr(view, "model", None) or model
        provider = getattr(view, "provider", None) or provider
        base_url = getattr(view, "base_url", None) or base_url
        api_mode = getattr(view, "api_mode", None) or api_mode
    try:
        fields = effort_fields(reasoning_config, "", model, provider, base_url, api_mode)
    except Exception:  # health: allow BLE001 -- 算不出来就当没有，界面只是少一句说明
        return ""
    return str(fields.get("reasoning_effort_wire") or "")
