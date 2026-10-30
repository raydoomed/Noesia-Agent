---
name: noesia-agent
description: "Use, configure, theme, extend, and orchestrate Noesia Agent."
version: 3.2.0
author: Noesia Agent + Teknium
license: MIT
platforms: [linux, macos, windows]
metadata:
  noesia:
    tags: [noesia, setup, configuration, multi-agent, spawning, cli, gateway, bots, bot-mode, features, themes, skins, desktop-plugins, tui-widgets, petdex, development]
    related_skills: [claude-code, codex, opencode]
---

# Noesia Agent

Noesia Agent is an open-source AI agent framework that runs in your terminal, a native desktop app, messaging platforms, and IDEs. It's in the same category as Claude Code (Anthropic), Codex (OpenAI), and OpenClaw — autonomous coding and task-execution agents that use tool calling to interact with your system. Noesia works with any LLM provider (OpenRouter, Anthropic, OpenAI, Google, DeepSeek, xAI, local models, and 20+ others) and runs on Linux, macOS, Windows, and WSL.

What makes Noesia different:

- **Self-improving through skills** — Noesia learns from experience by saving reusable procedures as skills that load into future sessions.
- **Persistent memory across sessions** — remembers who you are, your preferences, environment details, and lessons learned. Pluggable memory backends.
- **Multi-platform gateway** — the same agent runs on Telegram, Discord, Slack, WhatsApp, iMessage, Signal, Matrix, Teams, Email, and a dozen more platforms with full tool access, not just chat.
- **Many surfaces** — the same agent core drives the CLI, the Ink TUI, a native Electron desktop app, a web dashboard, and an ACP server for IDEs (VS Code / Zed / JetBrains).
- **Provider-agnostic** — swap models and providers mid-workflow; credential pools rotate across multiple API keys automatically.
- **Profiles** — run multiple independent Noesia instances with isolated configs, sessions, skills, and memory.
- **Extensible & themeable** — plugins, MCP servers, custom tools, webhook triggers, cron scheduling, skins that theme every surface, desktop UI plugins, TUI widgets, and pet mascots.

**This skill is a hub.** The body covers identity, quick start, spawning/orchestration, and hard invariants. Everything else lives in reference files — **load the matching reference (below) before answering**; do not answer detail questions from the body alone.

## Scope & Verification

This skill is a concise operating guide, not the complete source of truth for every Noesia feature. If a Noesia feature, command, or setting is not mentioned here or in a reference, do not treat that absence as evidence that it does not exist. Check this repository's source and `noesia --help` before giving a negative answer.

Good verification targets, cheapest first:

- CLI commands: `noesia --help`, `noesia <command> --help`, and `noesia_cli/main.py`
- Source tree: 本仓库 `src/engine/`；技能树在 `src/engine/skills/`

Never answer "Noesia can't do that" from memory. Noesia ships far more than this skill body describes, so a negative answer must first be checked in `noesia --help` or the reference files.

## Quick Start

```bash
# 从仓库安装：建 venv 后 `python -m pip install -e ".[dev]"`，双击仓库根目录的 Noesia.exe 启动

# Interactive chat (default surface; set display.interface: tui to launch the Ink TUI instead)
noesia

# Single query
noesia chat -q "What is the capital of France?"

# Setup wizard  /  pick model+provider  /  health check
noesia setup
noesia model
noesia doctor

# Other surfaces
noesia desktop                 # launch the native desktop app (alias: noesia gui)
noesia dashboard               # web admin panel + embedded chat
noesia proxy                   # OpenAI-compatible local proxy backed by your OAuth provider
```

## Key Paths

```
~/.noesia/config.yaml       Main configuration (settings — never secrets)
~/.noesia/.env              API keys and secrets ONLY (under $NOESIA_HOME if set)
$NOESIA_HOME/skills/        Installed skills
~/.noesia/skins/            Custom themes (see references/themes.md)
~/.noesia/desktop-plugins/  Desktop app UI plugins (see references/desktop-plugins.md)
~/.noesia/tui-widgets/      TUI widget apps (see references/tui-widgets.md)
~/.noesia/pets/             Installed pet mascots (see references/petdex.md)
~/.noesia/state.db          Canonical session store (SQLite + FTS5)
~/.noesia/sessions/         Gateway routing index, request dumps, *.jsonl transcripts
~/.noesia/logs/             Gateway and error logs
~/.noesia/auth.json         OAuth tokens and credential pools
~/.noesia/noesia-agent/     Source code (if git-installed)
```

Profiles use `~/.noesia/profiles/<name>/` with the same layout. When a profile is active, resolve the real home from `$NOESIA_HOME` — never hardcode `~/.noesia`.

## Routing Table — load the reference for the task

| User wants... | Load |
|---|---|
| **Anything not listed below — "can Noesia do X?", "how do I set up X?"** | 先 `noesia --help`，再查 `references/` 下同类文件 |
| Bots that chat, run routines, or message each other; the Bots tab | `references/cli-reference.md` |
| CLI commands, subcommands, flags, "how do I run X" | `references/cli-reference.md` |
| In-session slash commands | `references/slash-commands.md` |
| Provider setup, API keys, OAuth | `references/providers-and-models.md` |
| config.yaml sections, toolsets, voice/STT/TTS | `references/configuration.md` |
| AGENTS.md / .noesia.md / CLAUDE.md project rules | `references/project-context-files.md` |
| Secret redaction, PII, approval modes, "reset permissions" | `references/security-privacy.md` |
| Delegation, cron, curator, kanban | `references/background-systems.md` |
| MCP servers (add, catalog, `noesia mcp`) | `references/native-mcp.md` |
| Webhook routes and event-driven runs | `references/webhooks.md` |
| A custom theme/skin ("synthwave theme", "change the gold ●") | `references/themes.md` + `templates/skin.yaml` |
| A desktop app UI element (pane, widget, ⌘K command, page) | `references/desktop-plugins.md` + `templates/plugin.js` |
| A live TUI panel or modal widget (ticker, clock, dashboard) | `references/tui-widgets.md` + `templates/clock.mjs` |
| Pet mascots — install, select, scale, diagnose | `references/petdex.md` |
| Windows-specific issues (keybinds, WinError 10106, BOM) | `references/windows-quirks.md` |
| Debugging: voice, tools missing, gateway, aux models | `references/troubleshooting.md` |
| Contributing code: adding tools, slash commands, tests | `references/contributor-guide.md` |
| delegate_task "capped at N" reports | `references/delegate-task-concurrency-diagnosis.md` |
| Connecting a messaging platform (Telegram, Discord, Slack, WhatsApp, …) | `references/configuration.md` |

The reference list above is not the feature list — it is the set of topics that
need more than their docs page. For everything else Noesia ships, fetch
`llms.txt` and it maps the question to the page that answers it.

Two theming rules that hold even without loading the reference: **you apply skins yourself** (`noesia config set display.skin <name>` — every surface repaints live within ~a second; don't tell the user to run `/skin`), and **to tweak one color, edit the ACTIVE skin** (`noesia skin set <key> <hex>`) — never fork `default`, which drops the palette and resets the background.

## Spawning Additional Noesia Instances

Run additional Noesia processes as fully independent subprocesses — separate sessions, tools, and environments.

### When to Use This vs delegate_task

| | `delegate_task` | Spawning `noesia` process |
|-|-----------------|--------------------------|
| Isolation | Separate conversation, shared process | Fully independent process |
| Duration | Minutes (bounded by parent loop) | Hours/days |
| Tool access | Subset of parent's tools | Full tool access |
| Interactive | No | Yes (PTY mode) |
| Use case | Quick parallel subtasks | Long autonomous missions |

### One-Shot Mode

```
terminal(command="noesia chat -q 'Research GRPO papers and write summary to ~/research/grpo.md'", timeout=300)

# Background for long tasks:
terminal(command="noesia chat -q 'Set up CI/CD for ~/myapp'", background=true)
```

### Interactive PTY Mode (via tmux)

Noesia uses prompt_toolkit, which requires a real terminal. Use tmux for interactive spawning:

```
# Start
terminal(command="tmux new-session -d -s agent1 -x 120 -y 40 'noesia'", timeout=10)

# Wait for startup, then send a message
terminal(command="sleep 8 && tmux send-keys -t agent1 'Build a FastAPI auth service' Enter", timeout=15)

# Read output
terminal(command="sleep 20 && tmux capture-pane -t agent1 -p", timeout=5)

# Send follow-up
terminal(command="tmux send-keys -t agent1 'Add rate limiting middleware' Enter", timeout=5)

# Exit
terminal(command="tmux send-keys -t agent1 '/exit' Enter && sleep 2 && tmux kill-session -t agent1", timeout=10)
```

### Multi-Agent Coordination

```
# Agent A: backend
terminal(command="tmux new-session -d -s backend -x 120 -y 40 'noesia -w'", timeout=10)
terminal(command="sleep 8 && tmux send-keys -t backend 'Build REST API for user management' Enter", timeout=15)

# Agent B: frontend
terminal(command="tmux new-session -d -s frontend -x 120 -y 40 'noesia -w'", timeout=10)
terminal(command="sleep 8 && tmux send-keys -t frontend 'Build React dashboard for user management' Enter", timeout=15)

# Check progress, relay context between them
terminal(command="tmux capture-pane -t backend -p | tail -30", timeout=5)
terminal(command="tmux send-keys -t frontend 'Here is the API schema from the backend agent: ...' Enter", timeout=5)
```

### Session Resume

```
# Resume most recent session
terminal(command="tmux new-session -d -s resumed 'noesia --continue'", timeout=10)

# Resume specific session
terminal(command="tmux new-session -d -s resumed 'noesia --resume 20260225_143052_a1b2c3'", timeout=10)
```

### Tips

- **Prefer `delegate_task` for quick subtasks** — less overhead than spawning a full process
- **Use `-w` (worktree mode)** when spawning agents that edit code — prevents git conflicts
- **Set timeouts** for one-shot mode — complex tasks can take 5-10 minutes
- **Use `noesia chat -q` for fire-and-forget** — no PTY needed
- **Use tmux for interactive sessions** — raw PTY mode has `\r` vs `\n` issues with prompt_toolkit
- **For scheduled tasks**, use the `cronjob` tool instead of spawning — handles delivery and retry
- **"delegate_task is capped at N" reports** — see `references/delegate-task-concurrency-diagnosis.md`. Three real cap paths in Noesia; if none fired, the model is self-limiting and rationalising it as "the runtime caps."

## Surfaces (quick orientation)

- **Desktop app** (`noesia desktop` / `noesia gui`) — native Electron app for macOS/Linux/Windows: streaming chat, session list, Cmd+K palette, drag-and-drop files, native notifications, per-profile remote-gateway login. Extend it with UI plugins — `references/desktop-plugins.md`.
- **Web dashboard** (`noesia dashboard`) — full admin panel: messaging channels, MCP catalog, webhooks, memory, profile builder, plus an embedded `noesia --tui` chat. Secured behind an OAuth/token gate.
- **Ink TUI** (`noesia --tui` or `display.interface: tui`) — terminal UI with docked widget apps — `references/tui-widgets.md`.
- **OpenAI-compatible proxy** (`noesia proxy`) — a local OpenAI API backed by whichever OAuth provider you're signed into. Point Codex CLI, Aider, Cline, or any script at it — no API key.

## Hard Invariants (never violate, regardless of what you loaded)

- **Never break prompt caching** — don't change past context, toolsets, or the system prompt mid-conversation. The only exception is context compression.
- **Message role alternation** — never two assistant or two user messages in a row; only `tool` results can repeat.
- **Secrets in `.env`, settings in `config.yaml`** — never tell a user to put a non-credential setting in `.env`.
- **Profile-safe paths** — `get_noesia_home()` in code, `$NOESIA_HOME` when resolving paths in a session.
- **Never hand-edit `config.yaml` for the user** — use `noesia config set KEY VAL`; a stray indent can corrupt the file and break the live gateway.
