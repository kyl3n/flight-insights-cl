"""Ground station software installed on this computer.

Only local reads: the Windows uninstall registry, and on Linux the Flatpak,
AppImage and Mono install locations. A version that cannot be read is left
out rather than guessed.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FLATPAK_ID = "org.mavlink.qgroundcontrol"
VERSION = re.compile(r"(\d+\.\d+(?:\.\d+)*)")
RELEASE = re.compile(r'<release[^>]*\bversion="([^"]+)"')
NAMES = {"mission planner": "Mission Planner", "qgroundcontrol": "QGroundControl"}
UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
UNINSTALL_32 = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"


@dataclass(frozen=True)
class Installed:
    name: str  # "Mission Planner" or "QGroundControl"
    version: str | None
    where: str  # what it was read from

    def __str__(self) -> str:
        return f"{self.name} {self.version}" if self.version else self.name


def _order(i: Installed) -> tuple[str, tuple[int, ...]]:
    """By name, then version as numbers (1.3.82 after 1.3.9)."""
    return i.name, tuple(int(n) for n in re.findall(r"\d+", i.version or ""))


def _known(display_name: str) -> str | None:
    lowered = display_name.casefold()
    return next((name for key, name in NAMES.items() if key in lowered), None)


def _windows_registry() -> Iterator[tuple[str, str | None]]:
    """(DisplayName, DisplayVersion) of every program in the uninstall registry."""
    if sys.platform != "win32":
        return
    import winreg

    hives = ((winreg.HKEY_LOCAL_MACHINE, UNINSTALL), (winreg.HKEY_LOCAL_MACHINE, UNINSTALL_32),
             (winreg.HKEY_CURRENT_USER, UNINSTALL))  # fmt: skip
    for hive, path in hives:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as app:
                        name = str(winreg.QueryValueEx(app, "DisplayName")[0])
                        try:
                            version: str | None = str(winreg.QueryValueEx(app, "DisplayVersion")[0])
                        except OSError:
                            version = None
                except OSError:
                    continue
                yield name, version


def _from_registry(entries: Iterable[tuple[str, str | None]]) -> list[Installed]:
    found: list[Installed] = []
    for display_name, version in entries:
        if name := _known(display_name):
            found.append(Installed(name, version or None, "Windows uninstall registry"))
    return found


def _from_linux(home: Path, system: Path) -> list[Installed]:
    found: list[Installed] = []
    for base, where in ((home / ".local/share/flatpak", "Flatpak, user"),
                        (system / "var/lib/flatpak", "Flatpak, system")):  # fmt: skip
        app = base / "app" / FLATPAK_ID
        if not app.is_dir():
            continue
        version = None
        for info in sorted((app / "current/active/files/share").glob("*info/*.xml")):
            if m := RELEASE.search(info.read_text(encoding="utf-8", errors="replace")):
                version = m[1]
                break
        found.append(Installed("QGroundControl", version, where))
    for folder in (home, home / "Applications", home / "Downloads", system / "opt"):
        for image in sorted(folder.glob("QGroundControl*.AppImage")):
            m = VERSION.search(image.name.removeprefix("QGroundControl"))
            found.append(Installed("QGroundControl", m[1] if m else None, f"AppImage {image.name}"))
    for folder in (home / "MissionPlanner", system / "opt/MissionPlanner"):
        if (folder / "MissionPlanner.exe").is_file():
            found.append(Installed("Mission Planner", None, "Mission Planner under Mono"))
    return found


def installed_software(
    platform: str = sys.platform,
    home: Path | None = None,
    system: Path = Path("/"),
    registry: Iterable[tuple[str, str | None]] | None = None,
) -> list[Installed]:
    """Ground station programs installed here, each once."""
    if platform == "win32":
        found = _from_registry(_windows_registry() if registry is None else registry)
    elif platform.startswith("linux"):
        found = _from_linux(home or Path.home(), system)
    else:
        found = []
    unique: dict[tuple[str, str | None], Installed] = {}
    for item in found:
        unique.setdefault((item.name, item.version), item)
    return sorted(unique.values(), key=_order)


def software_answers(installed: list[Installed]) -> dict[str, dict[str, Any]]:
    """Answers for the software part, each with its source. The program and its
    version are filled only when exactly one ground station program is installed."""
    if not installed:
        return {}
    where = sorted({i.where for i in installed})
    out: dict[str, dict[str, Any]] = {
        "installed_gcs": {"value": "; ".join(map(str, installed)),
                          "source": f"detected: {', '.join(where)}"},
    }  # fmt: skip
    names = {i.name for i in installed}
    if len(names) == 1:
        only = installed[-1]  # the newest version when several are installed
        out["gcs"] = {"value": only.name, "source": f"detected: {only.where}"}
        if only.version:
            out["gcs_version"] = {"value": only.version, "source": f"detected: {only.where}"}
    return out
