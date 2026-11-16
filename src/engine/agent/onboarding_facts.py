"""The safe facts (class A) a first-run onboarding is built from, read once when the run starts.

No consent is needed for any of them: OS, architecture, machine kind, the NVIDIA platform and the
locale. They describe the machine running this backend; the desktop says when that is not the user's
own computer (``facts_source``).
"""

from __future__ import annotations

from agent.onboarding_flow import Facts

_OS = {"darwin": "macos", "win32": "windows"}


def read_facts(*, facts_source: str = "local", locale: str | None = None) -> Facts:
    """``locale``: the desktop's UI language, preferred over the backend OS locale for wording."""
    from noesia_platform.host.summary import summary

    host = summary()
    machine = host["machine"]
    return Facts(
        os=_OS.get(machine.get("os_family", ""), "linux"), arch=machine.get("native_arch", ""),
        machine_kind=host["machine_kind"], nvidia_platform=host["nvidia_platform"],
        facts_source="remote" if facts_source == "remote" else "local", locale=locale or host["locale"])


def suggested_name() -> str | None:
    """The OS account's real name for the name card. It goes to the card only: the model sees it
    when the user picks it."""
    from noesia_platform.host.summary import summary

    return summary()["full_name"]
