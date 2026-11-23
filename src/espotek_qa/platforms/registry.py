"""Which targets exist, and which platform (golden image) each runs on."""

from __future__ import annotations

from ..config import Config
from . import Platform, Target
from .linux import LinuxAppImageTarget, LinuxDebTarget, LinuxPlatform
from .macos import MacPlatform, MacTarget
from .windows import WindowsPlatform, WindowsTarget

PLATFORMS: dict[str, Platform] = {
    "linux": LinuxPlatform(),
    "windows": WindowsPlatform(),
    "macos": MacPlatform(),
}

TARGETS: dict[str, Target] = {
    t.id: t
    for t in (
        LinuxDebTarget(PLATFORMS["linux"]),
        LinuxAppImageTarget(PLATFORMS["linux"]),
        WindowsTarget(PLATFORMS["windows"]),
        MacTarget(PLATFORMS["macos"]),
    )
}


def platform(name: str) -> Platform:
    if name not in PLATFORMS:
        raise KeyError(f"unknown platform {name!r}; known: {sorted(PLATFORMS)}")
    return PLATFORMS[name]


def targets_for(cfg: Config, ids: list[str] | None) -> list[Target]:
    """The requested targets, or every target on an enabled platform."""
    unknown_platforms = [p for p in cfg.platforms if p not in PLATFORMS]
    if unknown_platforms:
        raise KeyError(f"config.toml enables unknown platforms {unknown_platforms}; known: {sorted(PLATFORMS)}")
    if ids is None:
        return [t for t in TARGETS.values() if t.platform.name in cfg.platforms]
    unknown = [i for i in ids if i not in TARGETS]
    if unknown:
        raise KeyError(f"unknown targets {unknown}; known: {sorted(TARGETS)}")
    return [TARGETS[i] for i in ids]
