"""Credential sections of `noesia status`, run through ``status._SECTIONS`` with its shared context.
Origin helpers (``_row``, ``_first_env_value``, ...) are resolved through the ``noesia_cli.status``
module object so tests that monkeypatch that module keep working."""

from noesia_cli.auth import AuthError
from noesia_cli import config


# Values may be a single env var name (str) or a tuple of alternates (first found wins).
_API_KEYS: dict[str, str | tuple[str, ...]] = {
    "OpenRouter": "OPENROUTER_API_KEY", "OpenAI": "OPENAI_API_KEY",
    "Google / Gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"), "DeepSeek": "DEEPSEEK_API_KEY",
    "xAI / Grok": "XAI_API_KEY", "NVIDIA NIM": "NVIDIA_API_KEY", "Z.AI / GLM": "GLM_API_KEY",
    "Kimi": "KIMI_API_KEY", "StepFun Step Plan": "STEPFUN_API_KEY", "MiniMax": "MINIMAX_API_KEY",
    "MiniMax-CN": "MINIMAX_CN_API_KEY", "DeepInfra": "DEEPINFRA_API_KEY", "Firecrawl": "FIRECRAWL_API_KEY",
    "Tavily": "TAVILY_API_KEY", "Perplexity": "PERPLEXITY_API_KEY", "Keenable": "KEENABLE_API_KEY",
    "Browser Use": "BROWSER_USE_API_KEY",  # Optional — local browser works without this
    "Browserbase": "BROWSERBASE_API_KEY",  # Optional — direct credentials only
    "FAL": "FAL_KEY", "ElevenLabs": "ELEVENLABS_API_KEY", "GitHub": "GITHUB_TOKEN"}

_APIKEY_PROVIDERS = {
    "Z.AI / GLM": ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"), "Kimi / Moonshot": ("KIMI_API_KEY",),
    "StepFun Step Plan": ("STEPFUN_API_KEY",), "MiniMax": ("MINIMAX_API_KEY",),
    "MiniMax (China)": ("MINIMAX_CN_API_KEY",), "DeepInfra": ("DEEPINFRA_API_KEY",)}

def _render_api_keys(ctx):
    _status._section("API Keys")
    from noesia_cli.auth import get_anthropic_key
    # Anthropic uses the dedicated lookup (it also resolves OAuth tokens).
    for name, env_ref in (*_API_KEYS.items(), ("Anthropic", get_anthropic_key)):
        value = env_ref() if callable(env_ref) else _status._first_env_value(env_ref)
        _status._row(name, bool(value), config.redact_key(value))


def _render_apikey_providers(ctx):
    _status._section("API-Key Providers")
    for pname, env_vars in _APIKEY_PROVIDERS.items():
        configured = bool(_status._first_env_value(env_vars))
        _status._row(pname, configured, "configured" if configured else "not configured (run: noesia model)", 16, " ")

    # LM Studio reachability: probe only when it is the active provider so users with foreign
    # configs see no noise. Auth rejection vs. a silent empty list is the common support case.
    if _status._effective_provider_label() == "LM Studio":
        from noesia_cli.models_local import probe_lmstudio_models
        model_cfg = ctx.config.get("model")
        base = ((model_cfg.get("base_url") if isinstance(model_cfg, dict) else None)
                or _status.get_env_value("LM_BASE_URL") or "http://127.0.0.1:1234/v1")
        try:
            models = probe_lmstudio_models(api_key=_status.get_env_value("LM_API_KEY") or "",
                                           base_url=base, timeout=1.5)
            ok = models is not None
            msg = f"reachable ({len(models)} model(s)) at {base}" if ok else f"unreachable at {base}"
        except AuthError:
            ok, msg = False, "auth rejected — set LM_API_KEY"
        _status._row("LM Studio", ok, msg, 16, " ")


import noesia_cli.status as _status
