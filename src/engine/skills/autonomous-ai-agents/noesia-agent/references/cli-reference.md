# Noesia CLI Reference

Live sources when anything looks stale: `noesia --help`, `noesia <command> --help`,

### Global Flags

```
noesia [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
noesia chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
noesia setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
noesia model                Interactive model/provider picker
noesia fallback [add|remove|list]  Fallback provider chain
noesia config [show|edit|get|set|unset|path|env-path|check|migrate]
noesia login / logout       OAuth sign-in / clear stored auth
noesia doctor [--fix]       Check dependencies and config
noesia status [--full]      Component summary (--full: every section)
```

### Tools & Skills

```
noesia tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

noesia skills list|browse|search QUERY|inspect ID
noesia skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
noesia skills config        Enable/disable skills per platform
noesia skills check|update|uninstall|publish PATH
noesia skills tap add REPO  Add a GitHub repo as a skill source
noesia bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
noesia mcp add NAME (--url or --command) | remove | list | test NAME
noesia mcp catalog | install NAME     Curated catalog install
noesia mcp configure NAME             Toggle tool selection
noesia mcp-serve                      Run Noesia as an MCP server (mcp-serve plugin)
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
noesia gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `noesia photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.

### Sessions

```
noesia sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
noesia cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
noesia webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
noesia profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
noesia profile rename A B | alias NAME | export NAME | import FILE
noesia profile migrate-identity A B   Retry a completed rename's session/routing identity migration
```

### Credentials & Pools

```
noesia auth                 Interactive credential manager
noesia auth add [PROVIDER]  Add OAuth or API-key credential (openai-codex, qwen-oauth, …)
noesia auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
noesia desktop / gui        Native desktop app
noesia dashboard            Web admin panel + embedded chat (--stop / --status)
noesia proxy                OpenAI-compatible local proxy backed by an OAuth provider
noesia kanban <verb>        Multi-agent work-queue board
noesia project              Named multi-folder workspaces
noesia skin list|use|set    Switch/tweak skins (see references/themes.md)
noesia pets <verb>          Pet mascots (see references/petdex.md)
noesia memory setup|status|off|reset   Memory provider
noesia secrets bitwarden|onepassword   External secret stores
noesia moa                  Mixture-of-Agents slots
noesia hooks / security / backup / import / checkpoints / console
noesia logs [-f] [errors]   View agent/error logs
noesia send                 One-off message through a gateway platform
noesia pairing / plugins / insights / journey / computer-use
noesia acp                  ACP server (IDE integration)
noesia completion bash|zsh|fish
noesia update / uninstall
```

Plugin- and provider-supplied subcommands (e.g. `noesia photon setup`) only appear once their plugin is installed/active. Importing from OpenClaw, Claude Code, Codex CLI or Cursor (`noesia import-agent <agent>`, plus the older `noesia claw migrate|cleanup`) comes from the `agent-importers` plugin; a fresh `noesia setup` that finds one of those agents offers to install it.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `noesia config edit`（详情 `noesia config --help`） |
| Tools / toolsets | `noesia tools list`（详情 `noesia tools --help`） |
| Skills catalog | `noesia skills browse`（详情 `noesia skills --help`） |
| Provider setup | `noesia model` · `references/providers-and-models.md` |
| Env variables | `noesia config env-path`（详情 `noesia config env-path --help`） |
| Gateway logs | `~/.noesia/logs/gateway.log` (or `noesia logs`) |
| Sessions | `noesia sessions browse` (reads state.db) |
