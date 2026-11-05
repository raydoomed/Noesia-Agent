"""``noesia doctor`` — diagnose (and with --fix, repair) a Noesia install.

``run_doctor`` walks ``DOCTOR_CHECKS`` in order; each check prints its own rows and returns a ``Finding``.
Check bodies live in the ``doctor_*`` siblings.
"""

import os
import sys

from noesia_cli.config import get_env_path, get_noesia_home, get_project_root
from noesia_cli.env_loader import load_noesia_dotenv
from noesia_constants import display_noesia_home

PROJECT_ROOT = get_project_root()
NOESIA_HOME = get_noesia_home()
_DHH = display_noesia_home()  # user-facing display path (e.g. ~/.noesia or ~/.noesia/profiles/coder)

# Load environment variables from ~/.noesia/.env so API key checks work
_env_path = get_env_path()
load_noesia_dotenv(noesia_home=_env_path.parent, project_env=PROJECT_ROOT / ".env")

from noesia_cli.colors import Colors, color
from noesia_cli.doctor_report import Finding, _section, doctor_check, warn_on_error
from noesia_cli.doctor_connectivity import build_probes, run_probes

from noesia_cli.doctor_config import (
    _check_config_drift,
    _check_config_file,
    _check_env_file,
    _check_mcp_security,
    _check_relay_plugins,
    _check_xai_retirement,
    _check_retired_session_reset,
)
from noesia_cli.doctor_platform import (
    _check_certificates,
    _check_command_installation,
    _check_gateway_supervision,
    _check_python_environment,
    _check_required_packages,
    _check_security_advisories,
    _check_web_dashboard_import,
)
from noesia_cli.doctor_tools import (
    _check_git_and_rg,
    _check_node_and_browser,
    _check_npm_audit,
    _check_terminal_backend,
    _check_tool_availability,
)
from noesia_cli.doctor_state import (
    _check_checkpoint_store,
    _check_cron_store,
    _check_directory_structure,
    _check_memory_provider,
    _check_profiles,
    _check_skills_hub,
    _check_state_db,
)

_PROVIDER_ENV_HINTS = (
    "DEEPINFRA_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    "OPENAI_BASE_URL", "NOESIA_API_KEY", "GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY", "KIMI_API_KEY",
    "KIMI_CN_API_KEY", "GMI_API_KEY", "FIREWORKS_API_KEY", "ACTUAL_API_KEY", "ACTUAL_BASE_URL", "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY", "KILOCODE_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "HF_TOKEN",
    "AI_GATEWAY_API_KEY", "OPENCODE_ZEN_API_KEY", "OPENCODE_GO_API_KEY", "COMMANDCODE_API_KEY", "XIAOMI_API_KEY",
    "TOKENHUB_API_KEY", "TOKENPLAN_API_KEY",
)

@doctor_check()
def _check_api_connectivity(should_fix: bool, f: Finding) -> None:
    """Parallel HTTP/SDK probes for every configured provider; results printed in submission order."""
    probes = build_probes()
    # Single status line so users see something happening; ``\r`` clears it once results land.
    print(f"  {color(f'Running {len(probes)} connectivity checks in parallel…', Colors.DIM)}", end="", flush=True)
    results = run_probes(probes)
    print("\r" + " " * 70 + "\r", end="")
    for r in results:
        for glyph, label, detail in r.lines:
            print(f"  {glyph} {label}" + (f" {detail}" if detail else ""))
        if r.issues:
            f.issues.extend(r.issues)


# Ordered (section title, check). None title = check prints its own header (or none); order is user-visible.
DOCTOR_CHECKS = (
    ('Security Advisories', _check_security_advisories), ('MCP Server Security', _check_mcp_security),
    ('Python Environment', _check_python_environment), ('SSL / CA Certificates', _check_certificates),
    ('Required Packages', _check_required_packages), (None, _check_web_dashboard_import),
    ('Configuration Files', _check_env_file),
    (None, _check_config_file), (None, _check_config_drift),
    ('xAI Model Retirement (May 15, 2026)', _check_xai_retirement),
    ('Session Reset (timers removed Sep 7, 2026)', _check_retired_session_reset),
    ('Directory Structure', _check_directory_structure), (None, _check_state_db), (None, _check_cron_store),
    (None, _check_checkpoint_store),
    (None, _check_gateway_supervision), (None, _check_command_installation),
    ('External Tools', _check_git_and_rg), (None, _check_terminal_backend), (None, _check_node_and_browser),
    (None, _check_npm_audit), ('API Connectivity', _check_api_connectivity),
    ('Tool Availability', _check_tool_availability), ('Skills Hub', _check_skills_hub),
    ('Memory Provider', _check_memory_provider), ('NeMo Relay Plugins', _check_relay_plugins),
    (None, _check_profiles),
)


def _ack_advisory(ack_target: str) -> None:
    """`noesia doctor --ack <id>`: persist the ack and return without running diagnostics."""
    from noesia_cli.security_advisories import ADVISORIES, ack_advisory
    valid_ids = {a.id for a in ADVISORIES}
    if ack_target not in valid_ids:
        print(color(f"Unknown advisory ID: {ack_target!r}. Known IDs: {', '.join(sorted(valid_ids)) or '(none)'}", Colors.RED))
        sys.exit(2)
    if ack_advisory(ack_target):
        print(color(f"  ✓ Acknowledged advisory {ack_target}. It will no longer trigger startup banners.", Colors.GREEN))
    else:
        print(color(f"  ✗ Could not save the acknowledgement for {ack_target}. Make sure {_DHH}/config.yaml is "
                    f"writable (`noesia config path` prints the exact file), then re-run "
                    f"`noesia doctor --ack {ack_target}`.", Colors.RED))
        sys.exit(1)


def _print_summary(should_fix: bool, total: Finding) -> None:
    print()
    remaining = total.issues + total.manual_issues
    numbered = "".join(f"  {i}. {issue}\n" for i, issue in enumerate(remaining, 1))
    if should_fix and total.fixed > 0:
        print(color("─" * 60, Colors.GREEN))
        print(color(f"  Fixed {total.fixed} issue(s).", Colors.GREEN, Colors.BOLD), end="")
        print(color(f" {len(remaining)} issue(s) require manual intervention.", Colors.YELLOW, Colors.BOLD) if remaining else "")
        print()
        if remaining:
            print(numbered)
    elif remaining:
        print(color("─" * 60, Colors.YELLOW))
        print(color(f"  Found {len(remaining)} issue(s) to address:", Colors.YELLOW, Colors.BOLD))
        print()
        print(numbered)
        if not should_fix and total.issues:
            print(color("  Tip: run 'noesia doctor --fix' to auto-fix what's possible.", Colors.DIM))
    else:
        print(color("─" * 60, Colors.GREEN))
        print(color("  All checks passed! 🎉", Colors.GREEN, Colors.BOLD))
    print()


def run_doctor(args):
    """Run diagnostic checks."""
    should_fix = getattr(args, 'fix', False)
    # Doctor runs from the interactive CLI, so CLI-gated tool checks (e.g. cronjob) see the same context.
    os.environ.setdefault("NOESIA_INTERACTIVE", "1")
    if getattr(args, 'ack', None):
        return _ack_advisory(args.ack)
    print()
    for line in ("┌─────────────────────────────────────────────────────────┐",
                 "│                 🩺 Noesia Doctor                        │",
                 "└─────────────────────────────────────────────────────────┘"):
        print(color(line, Colors.CYAN))
    total = Finding()
    for title, check in DOCTOR_CHECKS:
        if title:
            _section(title)
        total.merge(check(should_fix))
    # Opt-in live probes run AFTER all static checks (`--live`: real network calls; bounded + read-only).
    with warn_on_error(""):
        from noesia_cli.doctor_live import maybe_run_live_checks
        maybe_run_live_checks(args, total.manual_issues)
    _print_summary(should_fix, total)
    return int(bool(total.issues or total.manual_issues))
