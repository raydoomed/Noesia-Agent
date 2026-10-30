"""Directories where tools install outside PATH, grouped by the ecosystem that owns them.

Every table in Noesia lives here. A directory literal outside this module fails the ratchet in
`tests/test_managed_runtime_resolution.py`. Each table is empty on an OS where the ecosystem
does not install there, so callers compose tables without OS branches.
"""

from __future__ import annotations

import sys
from pathlib import Path

_POSIX = sys.platform != "win32"
_WIN = sys.platform == "win32"


def homebrew_dirs() -> tuple[str, ...]:
    return ("/opt/homebrew/bin", "/usr/local/bin") if sys.platform == "darwin" else ()


def user_local_bin() -> tuple[str, ...]:
    return ("~/.local/bin",) if _POSIX else ("%USERPROFILE%/.local/bin",)


def rust_tool_dirs() -> tuple[str, ...]:
    return ("~/.cargo/bin",) if _POSIX else ("%USERPROFILE%/.cargo/bin",)


def node_tool_dirs() -> tuple[str, ...]:
    return ("~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin") if _POSIX else ("%APPDATA%/npm", "%USERPROFILE%/.bun/bin", "%LOCALAPPDATA%/Volta/bin")


def noesia_vendored_dirs() -> tuple[str, ...]:
    return ("~/.noesia/bin",) if _POSIX else ("%USERPROFILE%/.noesia/bin",)


def machine_marker_files() -> tuple[str, ...]:
    """Where Noesia records that this OS user has used it: inside the data root in use.

    One product, one data root. Deleting that root has to mean "fresh install" — a marker in a
    machine-wide folder would outlive the profile it describes, so a wiped home would still count
    as "used" and onboarding would never run. Keep it next to the data it talks about.
    """
    from noesia_constants import get_default_noesia_root

    return (str(Path(get_default_noesia_root()) / ".machine"),)


def noesia_home_globs() -> tuple[str, ...]:
    """Other data roots to look at. Empty: this product has exactly one root, and `NOESIA_HOME`
    names it (the launch root is already a candidate). Globbing default folders let a stray,
    unrelated home elsewhere on the machine mark this install as "used"."""
    return ()


def gateway_service_definition_globs() -> tuple[str, ...]:
    """This OS user's gateway service definitions, each naming the NOESIA_HOME it serves."""
    if _WIN:
        return ("%APPDATA%/Microsoft/Windows/Start Menu/Programs/Startup/Noesia_Gateway*",)
    if sys.platform == "darwin":
        return ("~/Library/LaunchAgents/ai.noesia.gateway*.plist",)
    return ("$XDG_CONFIG_HOME/systemd/user/noesia-gateway*.service", "~/.config/systemd/user/noesia-gateway*.service")


def system_gateway_service_definition_globs() -> tuple[str, ...]:
    """Machine-wide gateway services. Each runs as the account its ``User=`` names (root when none)."""
    return ("/etc/systemd/system/noesia-gateway*.service",) if _POSIX and sys.platform != "darwin" else ()


def mac_privacy_protected_dirs() -> tuple[str, ...]:
    """Folders macOS guards with a permission prompt (TCC); opening anything inside raises it."""
    if sys.platform != "darwin":
        return ()
    return ("~/Documents", "~/Desktop", "~/Downloads", "~/Library/Mobile Documents")  # windows-footgun: ok — macOS only


def windows_user_program_dirs() -> tuple[str, ...]:
    if not _WIN:
        return ()
    return (
        "%LOCALAPPDATA%/Programs",
        "%USERPROFILE%/scoop/shims",
        "%LOCALAPPDATA%/Microsoft/WinGet/Links",
    )


def visual_studio_installer_dirs() -> tuple[str, ...]:
    """Where the Visual Studio installer puts vswhere.exe; it is never on PATH."""
    return ("%ProgramFiles(x86)%/Microsoft Visual Studio/Installer",) if _WIN else ()


def mac_application_dirs() -> tuple[str, ...]:
    return ("/Applications", "~/Applications") if sys.platform == "darwin" else ()


def flatpak_export_dirs() -> tuple[str, ...]:
    """Where flatpak links each installed app's launcher, named by its app id (system, then per-user)."""
    return ("/var/lib/flatpak/exports/bin", "~/.local/share/flatpak/exports/bin") if sys.platform.startswith("linux") else ()


def snap_bin_dirs() -> tuple[str, ...]:
    return ("/snap/bin",) if sys.platform.startswith("linux") else ()
