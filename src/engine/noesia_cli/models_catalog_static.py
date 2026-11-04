"""Provider registry (data only — no network, no model lists).

Canonical provider slugs/labels, display groups and provider alias maps. 模型清单不在这里：
一张静态模型名单都没有，谁能用哪些模型只由引擎线上现取（`model.options`）。
"""

from __future__ import annotations

from typing import NamedTuple


# ---------------------------------------------------------------------------
# Canonical provider list — single source of truth for provider identity. Every code path that
# lists, displays, or iterates providers (noesia model, /model, list_authenticated_providers)
# derives from it. slug = internal ID (config.yaml, --provider); label = short display name;
# tui_desc = longer description for the `noesia model` picker.
# ---------------------------------------------------------------------------

class ProviderEntry(NamedTuple):
    slug: str
    label: str
    tui_desc: str


CANONICAL_PROVIDERS: list[ProviderEntry] = [ProviderEntry(*row) for row in (
    ("fireworks", "Fireworks AI", "Fireworks AI (OpenAI-compatible direct model API)"),
    ("openrouter", "OpenRouter", "OpenRouter (Pay-per-use API aggregator)"),
    ("moa", "Mixture of Agents", "Mixture of Agents (named presets; aggregator acts after reference models)"),
    ("novita", "NovitaAI", "NovitaAI (Cloud: Model API, Agent Sandbox, GPU Cloud)"),
    ("lmstudio", "LM Studio", "LM Studio (Local desktop app with built-in model server)"),
    ("anthropic", "Anthropic", "Anthropic (Claude models via API key or Claude Code)"),
    ("openai-codex", "ChatGPT or Codex Subscription", "ChatGPT or Codex Subscription (Sign in with your ChatGPT account, uses Codex models)"),
    ("openai-api", "OpenAI API", "OpenAI API (api.openai.com, API key)"),
    ("alibaba", "Qwen Cloud", "Qwen Cloud / DashScope (Qwen + multi-provider)"),
    ("xai-oauth", "xAI Grok OAuth (SuperGrok / Premium+)", "xAI Grok OAuth (SuperGrok / Premium+ subscription)"),
    ("xiaomi", "Xiaomi MiMo", "Xiaomi MiMo (MiMo-V2.5 and V2 models: pro, omni, flash)"),
    ("tencent-tokenhub", "Tencent TokenHub", "Tencent TokenHub (Hy4 preview via tokenhub.tencentmaas.com)"),
    ("tencent-tokenplan", "Tencent TokenPlan", "Tencent TokenPlan (Hy4 preview via api.lkeap.cloud.tencent.com, Anthropic Messages)"),
    ("nvidia", "NVIDIA NIM", "NVIDIA NIM (Nemotron models via build.nvidia.com or local NIM)"),
    ("copilot", "GitHub Copilot", "GitHub Copilot (Uses GITHUB_TOKEN or gh auth token)"),
    ("copilot-acp", "GitHub Copilot ACP", "GitHub Copilot ACP (Spawns copilot --acp --stdio)"),
    ("huggingface", "Hugging Face", "Hugging Face Inference Providers"),
    ("gemini", "Google AI Studio", "Google AI Studio (Native Gemini API)"),
    ("vertex", "Google Vertex AI", "Google Vertex AI (Gemini via GCP; OAuth2 service account or ADC, GCP billing/quotas)"),
    ("deepseek", "DeepSeek", "DeepSeek (V3, R1, coder, direct API)"), ("xai", "xAI", "xAI Grok (Direct API)"),
    ("zai", "Z.AI / GLM", "Z.AI / GLM (Zhipu direct API)"),
    ("kimi-coding", "Kimi / Kimi Coding Plan", "Kimi Coding Plan (api.kimi.com & Moonshot API)"),
    ("kimi-coding-cn", "Kimi / Moonshot (China)", "Kimi / Moonshot China (Domestic direct API)"),
    ("stepfun", "StepFun Step Plan", "StepFun Step Plan (Agent / coding models via Step Plan API)"),
    ("minimax", "MiniMax", "MiniMax (Global direct API)"),
    ("minimax-oauth", "MiniMax (OAuth)", "MiniMax via OAuth browser login (Coding Plan, minimax.io)"),
    ("minimax-cn", "MiniMax (China)", "MiniMax China (Domestic direct API)"),
    ("ollama-cloud", "Ollama Cloud", "Ollama Cloud (Cloud-hosted open models, ollama.com)"),
    ("arcee", "Arcee AI", "Arcee AI (Trinity models, direct API)"),
    ("gmi", "GMI Cloud", "GMI Cloud (Multi-model direct API)"),
    ("kilocode", "Kilo Code", "Kilo Code (Kilo Gateway API)"),
    ("opencode-zen", "OpenCode Zen", "OpenCode Zen (Curated models, pay-as-you-go)"),
    ("opencode-go", "OpenCode Go", "OpenCode Go (Open models subscription)"),
    ("bedrock", "AWS Bedrock", "AWS Bedrock (Claude, Nova, Llama, DeepSeek; IAM or API key)"),
    ("azure-foundry", "Azure Foundry", "Azure Foundry (OpenAI-style or Anthropic-style endpoint, your Azure AI deployment)"),
    ("ai-gateway", "Vercel AI Gateway", "Vercel AI Gateway (Multi-model aggregator)"),
    ("qwen-oauth", "Qwen OAuth (Portal)", "Qwen OAuth (Reuses local Qwen CLI login)"),
)]


# Auto-extend CANONICAL_PROVIDERS with providers registered under plugins/model-providers/<name>/
# so a new provider reaches the picker, /model and every downstream consumer without edits here.
# Admission is by slug only: every in-tree non-api-key profile (OAuth, external-process, cloud
# SDK) already owns a hand-written row above, so the old auth_type skip set never excluded an
# in-tree provider — it only hid out-of-tree plugins. Visibility is gated downstream by
# credentials, not here: ``models._provider_has_credentials`` / ``_lap_canonical_rows`` route
# through ``auth.get_auth_status`` (external_process → the binary resolves; OAuth → auth.json /
# credential-pool entry), so an admitted row reads authenticated=False until the user signs in.
_canonical_slugs = {p.slug for p in CANONICAL_PROVIDERS}


def _plugin_provider_enters_picker(pp) -> bool:
    """Picker admission for a plugin model-provider profile: any slug without a built-in row.

    Every profile is admitted, hidden (pre-release) ones included: ``CANONICAL_PROVIDERS`` is also
    the typed-path registry (``provider:model`` parsing, labels), so a signed-in user must still be
    able to name one. The pickers that list rows filter with ``providers.provider_listed``."""
    return pp.name not in _canonical_slugs


def sync_plugin_provider_catalog() -> int:
    """Admit every registered plugin provider without a built-in row; return how many were added.

    Runs at import and again from ``providers._sync_auth_registry`` whenever a profile is registered
    after this module was imported. The import-time pass alone observes a *partial* registry: a
    plugin whose own imports pull ``noesia_cli.models`` in mid-``_discover_providers()``, or a
    profile registered later at runtime, would otherwise never reach the picker, ``noesia model``,
    ``/model`` or the Desktop ``model.options`` list until restart — the catalog twin of the auth
    registry window (#102123). Idempotent by slug; built-in rows are never rewritten.
    """
    try:
        from providers import list_providers
        profiles = list_providers()
    except Exception:
        return 0
    added = 0
    for pp in profiles:
        if not _plugin_provider_enters_picker(pp):
            continue
        label = pp.display_name or pp.name
        CANONICAL_PROVIDERS.append(ProviderEntry(pp.name, label, pp.description or f"{label} (direct API)"))
        _canonical_slugs.add(pp.name)
        _PROVIDER_LABELS[pp.name] = label
        added += 1
    return added


_PROVIDER_LABELS: dict[str, str] = {p.slug: p.label for p in CANONICAL_PROVIDERS}
_PROVIDER_LABELS["custom"] = "Custom endpoint"  # special case: not a named provider
sync_plugin_provider_catalog()


def listed_canonical_providers() -> list[ProviderEntry]:
    """``CANONICAL_PROVIDERS`` minus pre-release profiles the user has not opted into: what a provider
    LIST offers. Typed paths (``provider:model``, labels, ``--provider``) keep the full table."""
    from noesia_cli import models  # the binding every list consumer read before this filter existed
    from providers import unlisted_provider_names
    hidden = unlisted_provider_names()
    return [p for p in models.CANONICAL_PROVIDERS if p.slug not in hidden]


# ---------------------------------------------------------------------------
# Provider groups — DISPLAY ONLY. Vendors with several slugs (global API, China API, OAuth plan,
# ...) fold under one top-level row in the INTERACTIVE PICKERS (``noesia model``, setup wizard,
# Telegram ``/model``). They do NOT change CANONICAL_PROVIDERS, slug identity, ``--provider``,
# ``/model <provider:model>`` or any typed path — every member slug stays individually addressable.
# ``group_providers()`` is the single fold used by all three surfaces.
#   group_id -> (display_label, group_description shown on the collapsed row, [member_slug, ...])
# Member order is the order shown inside the group submenu; member detail lives in ``tui_desc``.
# ---------------------------------------------------------------------------
PROVIDER_GROUPS: dict[str, tuple[str, str, list[str]]] = {
    "kimi":     ("Kimi / Moonshot", "Coding Plan, Moonshot global & China endpoints", ["kimi-coding", "kimi-coding-cn"]),
    "minimax":  ("MiniMax",         "Global, OAuth Coding Plan & China endpoints",     ["minimax", "minimax-oauth", "minimax-cn"]),
    "xai":      ("xAI Grok",        "Direct API or SuperGrok / Premium+ OAuth",        ["xai", "xai-oauth"]),
    "google":   ("Google Gemini",   "Google AI Studio (API key)",                     ["gemini"]),
    "openai":   ("OpenAI",          "ChatGPT/Codex subscription or direct OpenAI API", ["openai-codex", "openai-api"]),
    "qwen":     ("Qwen",            "Qwen Cloud / DashScope, Coding Plan, Token Plan & Qwen CLI OAuth", ["alibaba", "alibaba-cn", "alibaba-coding-plan", "alibaba-coding-plan-cn", "alibaba-token-plan", "alibaba-token-plan-cn", "qwen-oauth"]),
    "opencode": ("OpenCode",        "Zen pay-as-you-go or Go subscription", ["opencode-zen", "opencode-go"]),
    "copilot":  ("GitHub Copilot",  "GitHub token API or copilot --acp process",       ["copilot", "copilot-acp"]),
    "tencent":  ("Tencent Hy",      "Hy4 / Hy3 via TokenHub & TokenPlan", ["tencent-tokenhub", "tencent-tokenplan"]),
}

# Reverse index: member slug -> group_id.
_SLUG_TO_GROUP: dict[str, str] = {
    slug: gid for gid, (_label, _desc, members) in PROVIDER_GROUPS.items() for slug in members
}


def provider_group_for_slug(slug: str) -> str:
    """Return the group_id a provider slug belongs to, or "" if ungrouped."""
    return _SLUG_TO_GROUP.get(str(slug or "").strip().lower(), "")


def group_providers(slugs):
    """Fold a flat ordered slug iterable into picker rows by provider group (DISPLAY ONLY).

    A group row appears at the position of its FIRST present member, in input order; later
    members fold into it. Member order inside a group follows ``PROVIDER_GROUPS`` declaration,
    restricted to the members present in ``slugs``.
    """
    present = set(slugs)
    group_members = {
        gid: [m for m in members if m in present]
        for gid, (_label, _desc, members) in PROVIDER_GROUPS.items()
    }
    rows = []
    seen: set[str] = set()
    emitted_groups: set[str] = set()
    for slug in slugs:
        s = str(slug or "").strip().lower()
        if not s or s in seen:
            continue
        seen.add(s)
        gid = _SLUG_TO_GROUP.get(s, "")
        if not gid:
            rows.append({"kind": "single", "slug": s})
            continue
        if gid in emitted_groups:
            continue  # already folded at the first member's position
        emitted_groups.add(gid)
        members = group_members.get(gid) or [s]
        if len(members) <= 1:
            rows.append({"kind": "single", "slug": members[0]})
        else:
            label, desc, _ = PROVIDER_GROUPS[gid]
            rows.append({"kind": "group", "group_id": gid, "label": label,
                         "description": desc, "members": list(members)})
    return rows


_PROVIDER_ALIASES = dict((
    ("glm", "zai"), ("z-ai", "zai"), ("z.ai", "zai"), ("zhipu", "zai"), ("github", "copilot"),
    ("github-copilot", "copilot"), ("github-models", "copilot"), ("github-model", "copilot"),
    ("github-copilot-acp", "copilot-acp"), ("copilot-acp-agent", "copilot-acp"), ("google", "gemini"),
    ("google-gemini", "gemini"), ("google-ai-studio", "gemini"), ("google-vertex", "vertex"), ("vertex-ai", "vertex"),
    ("gcp-vertex", "vertex"), ("vertexai", "vertex"), ("kimi", "kimi-coding"), ("moonshot", "kimi-coding"),
    ("kimi-cn", "kimi-coding-cn"), ("moonshot-cn", "kimi-coding-cn"), ("step", "stepfun"),
    ("stepfun-coding-plan", "stepfun"), ("arcee-ai", "arcee"), ("arceeai", "arcee"), ("gmi-cloud", "gmi"),
    ("gmicloud", "gmi"), ("fireworks-ai", "fireworks"), ("fw", "fireworks"), ("actual-computer", "actual"),
    ("actualcomputer", "actual"), ("aci", "actual"), ("nebius", "nebius-token-factory"),
    ("nebius-tokenfactory", "nebius-token-factory"), ("nebius-tf", "nebius-token-factory"),
    ("token-factory", "nebius-token-factory"), ("tokenfactory", "nebius-token-factory"),
    ("minimax-china", "minimax-cn"), ("minimax_cn", "minimax-cn"), ("minimax-portal", "minimax-oauth"),
    ("minimax-global", "minimax-oauth"), ("minimax_oauth", "minimax-oauth"), ("claude", "anthropic"),
    ("claude-code", "anthropic"), ("deep-seek", "deepseek"), ("opencode", "opencode-zen"), ("zen", "opencode-zen"),
    ("go", "opencode-go"), ("opencode-go-sub", "opencode-go"), ("aigateway", "ai-gateway"), ("vercel", "ai-gateway"),
    ("vercel-ai-gateway", "ai-gateway"), ("kilo", "kilocode"), ("kilo-code", "kilocode"),
    ("kilo-gateway", "kilocode"), ("dashscope", "alibaba"), ("aliyun", "alibaba"), ("qwen", "alibaba"),
    ("alibaba-cloud", "alibaba"), ("qwen-portal", "qwen-oauth"), ("hf", "huggingface"),
    ("hugging-face", "huggingface"), ("huggingface-hub", "huggingface"), ("novita-ai", "novita"),
    ("novitaai", "novita"), ("mimo", "xiaomi"), ("xiaomi-mimo", "xiaomi"), ("tencent", "tencent-tokenhub"),
    ("tokenhub", "tencent-tokenhub"), ("tencent-cloud", "tencent-tokenhub"), ("tencentmaas", "tencent-tokenhub"),
    ("tokenplan", "tencent-tokenplan"), ("tencent-lkeap", "tencent-tokenplan"), ("aws", "bedrock"),
    ("aws-bedrock", "bedrock"), ("amazon-bedrock", "bedrock"), ("amazon", "bedrock"), ("grok", "xai"),
    ("grok-oauth", "xai-oauth"), ("xai-oauth", "xai-oauth"), ("x-ai-oauth", "xai-oauth"),
    ("xai-grok-oauth", "xai-oauth"), ("x-ai", "xai"), ("x.ai", "xai"), ("nim", "nvidia"), ("nvidia-nim", "nvidia"),
    ("build-nvidia", "nvidia"), ("nemotron", "nvidia"), ("lmstudio", "lmstudio"), ("lm-studio", "lmstudio"),
    ("lm_studio", "lmstudio"), ("chatgpt", "openai-codex"), ("chatgpt-codex", "openai-codex"),
    ("ollama", "custom"),  # bare "ollama" = local; use "ollama-cloud" for cloud
    ("ollama_cloud", "ollama-cloud"),
    # Local OpenAI-compatible servers route through the generic "custom" provider
    # (parity with noesia_cli.auth and noesia_cli.providers). Issue #62213. The llamacpp
    # aliases stay unmapped: they are the managed local runtime's picker id, and the model
    # validator must reach its staged-library branch before the custom one.
    ("local", "custom"), ("vllm", "custom"),
))


# Offline/fresh-install fallback for the model Noesia silently lands on when the user never picked
# one (GUI onboarding confirm card, empty ``model.default``, provider-set-but-model-missing). The
_AGGREGATOR_PROVIDERS = frozenset({"openrouter", "ai-gateway", "copilot", "kilocode"})


# Subscription/OAuth providers whose catalogs RE-EXPOSE other vendors' models; tried only as a last
# resort for bare short-alias resolution (after every native-vendor catalog) so they never hijack
# an alias from the model's native vendor. None currently defined.
_BORROWED_MODEL_PROVIDERS: frozenset[str] = frozenset()


# Providers whose live /v1/models is the authoritative catalog: the picker merges live-first (live
# entries lead, curated-only append). Every OTHER provider keeps curated-first so a deliberately
# surfaced newest model stays on top when the live API lags. Zen/Go re-expose dozens of vendors
# and rotate them often, so their stale curated entries must not pollute the top.
_LIVE_FIRST_PICKER_PROVIDERS: frozenset[str] = frozenset({"opencode-zen", "opencode-go", "meta-ai"})


# Models supporting OpenAI Priority Processing (service_tier="priority"; see
# openai.com/api-priority-processing). Pattern-based: any OpenAI flagship (gpt-*, o1*, o3*, o4*).
# Non-OpenAI endpoints (OpenRouter/Copilot/opencode-zen proxies) strip service_tier, so false
# positives are harmless. Codex-series models are excluded — the Codex Responses API doesn't
# expose service_tier.
_OPENAI_FAST_MODE_PREFIXES: tuple[str, ...] = ("gpt-", "o1", "o3", "o4")
# OpenAI Ultrafast (service_tier="ultrafast", 6x Standard): GPT-6 Astra and GPT-6.1 Sol
# (developers.openai.com/api/docs/guides/ultrafast-mode, 2026-10-08).
# Exact wire slugs, matched after stripping the vendor prefix and the Noesia-side ``-900k`` alias.
_OPENAI_ULTRAFAST_MODELS: frozenset[str] = frozenset({"gpt-6-astra", "gpt-6.1-sol"})


# models.dev 的目录对每一家都生效（上游上新就出现、标 deprecated 就消失），只有 openrouter 例外：
# 我们给它的清单是手工挑过的 agentic 子集，把 models.dev 那 400+ 全倒进菜单等于毁掉这份精选；
# 而它本来就有自己的实时清单（``fetch_openrouter_models``），不需要靠 models.dev 更新。
_MODELS_DEV_MERGE_EXCLUDED: frozenset[str] = frozenset({"openrouter"})


# OpenRouter-style ids -> Copilot ids. Dash-notation Claude ids are accepted too: Noesia' default
# Claude IDs use hyphens (Anthropic native) but Copilot's API only accepts dot-notation, so a
# copilot + hyphenated default would otherwise hit HTTP 400 "model_not_supported".
_COPILOT_MODEL_ALIASES = dict((
    ("openai/gpt-5", "gpt-5-mini"), ("openai/gpt-5-chat", "gpt-5-mini"), ("openai/gpt-5-mini", "gpt-5-mini"),
    ("openai/gpt-5-nano", "gpt-5-mini"), ("openai/gpt-4.1", "gpt-4.1"), ("openai/gpt-4.1-mini", "gpt-4.1"),
    ("openai/gpt-4.1-nano", "gpt-4.1"), ("openai/gpt-4o", "gpt-4o"), ("openai/gpt-4o-mini", "gpt-4o-mini"),
    ("openai/o1", "gpt-5.2"), ("openai/o1-mini", "gpt-5-mini"), ("openai/o1-preview", "gpt-5.2"),
    ("openai/o3", "gpt-5.3-codex"), ("openai/o3-mini", "gpt-5-mini"), ("openai/o4-mini", "gpt-5-mini"),
    ("anthropic/claude-opus-4.6", "claude-opus-4.6"), ("anthropic/claude-sonnet-5", "claude-sonnet-5"),
    ("anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"), ("anthropic/claude-sonnet-4", "claude-sonnet-4"),
    ("anthropic/claude-sonnet-4.5", "claude-sonnet-4.5"), ("anthropic/claude-haiku-4.5", "claude-haiku-4.5"),
    ("claude-sonnet-5", "claude-sonnet-5"), ("claude-opus-4-6", "claude-opus-4.6"),
    ("claude-sonnet-4-6", "claude-sonnet-4.6"), ("claude-sonnet-4-0", "claude-sonnet-4"),
    ("claude-sonnet-4-5", "claude-sonnet-4.5"), ("claude-haiku-4-5", "claude-haiku-4.5"),
    ("anthropic/claude-opus-4-6", "claude-opus-4.6"), ("anthropic/claude-sonnet-4-6", "claude-sonnet-4.6"),
    ("anthropic/claude-sonnet-4-0", "claude-sonnet-4"), ("anthropic/claude-sonnet-4-5", "claude-sonnet-4.5"),
    ("anthropic/claude-haiku-4-5", "claude-haiku-4.5"),
))


# Azure Foundry model families that require the Responses API: Azure rejects /chat/completions
# against them with ``400 "The requested operation is unsupported."`` (seen on gpt-5.3-codex while
# gpt-4o on the same endpoint worked). Broad enough for vendor-renamed deployments (gpt-5.x-codex,
# o1-preview), tight enough to leave GPT-4 / 3.5 / Llama / Mistral / Grok on chat completions.
_AZURE_FOUNDRY_RESPONSES_PREFIXES = ("codex", "gpt-5", "o1", "o3", "o4")
