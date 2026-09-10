"""Default locations for the bundled ``nsight-systems`` skill pack.

Release installers should place the prebuilt skill pack in one of these
locations so user-facing commands can stay short. Source checkouts can still
pass ``--skill-pack`` explicitly.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from nsys_agent_tools.skill_pack import SkillPackError, load_manifest

from .defaults import NSYS_AGENT_SKILL_PACK_ENV

SKILL_PACK_NAME = "nsight-systems"
PROJECT_DIR_NAME = "nsys-agent-tools"

AGENT_SKILL_ROOTS: dict[str, Path] = {
    "claude": Path(".claude") / "skills",
    "codex": Path(".codex") / "skills",
    "agents": Path(".agents") / "skills",
}


class SkillPackPathError(ValueError):
    """Raised when a command needs a skill pack but none is configured."""


def resolve_skill_pack_path(value: str | Path | None = None) -> Path:
    """Resolve an explicit or release-installed skill-pack path.

    The search order is intentionally small and predictable:

    1. the explicit ``--skill-pack`` value;
    2. ``NSYS_AGENT_SKILL_PACK`` for wrappers/test harnesses;
    3. user data dir installed by release bundles; and
    4. Python prefix ``share`` dir installed by packaged distributions.
    """

    if value:
        return _configured_skill_pack_path(value, "--skill-pack")
    env_value = os.environ.get(NSYS_AGENT_SKILL_PACK_ENV)
    if env_value:
        return _configured_skill_pack_path(env_value, NSYS_AGENT_SKILL_PACK_ENV)
    for candidate in default_skill_pack_candidates():
        if _looks_like_skill_pack(candidate):
            return candidate.resolve()
    raise SkillPackPathError(
        "No bundled nsight-systems skill pack was found. Install the "
        "nsys-agent-tools release package, or pass --skill-pack when running "
        "from source. nsys_skill_cli does not build skill packs at runtime."
    )


def default_skill_pack_candidates() -> tuple[Path, ...]:
    """Return release-install locations checked by ``nsys_skill_cli``.

    Paths are returned even when they do not exist; this keeps diagnostics and
    tests deterministic without probing unrelated directories.
    """

    candidates: list[Path] = []
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        candidates.append(_skill_path(Path(xdg_data_home).expanduser()))
    candidates.append(_skill_path(Path.home() / ".local" / "share"))
    candidates.append(_skill_path(Path(sys.prefix) / "share"))
    return tuple(_dedupe_paths(candidates))


def agent_installed_packs(
    name: str = SKILL_PACK_NAME,
    roots: Iterable[Path] | None = None,
) -> tuple[Path, ...]:
    """Return resolved skill packs installed under the agent skill roots."""
    bases = tuple(roots) if roots is not None else (Path.home(), Path.cwd())
    candidates: list[Path] = []
    if env_value := os.environ.get(NSYS_AGENT_SKILL_PACK_ENV):
        candidates.append(Path(env_value).expanduser())
    candidates.extend(
        base / skill_root / name
        for base in bases
        for skill_root in AGENT_SKILL_ROOTS.values()
    )
    found: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if not _looks_like_skill_pack(path):
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            found.append(resolved)
    return tuple(found)


def pack_version(pack_dir: Path) -> str:
    """Return skill pack's package_version or "unknown" if unreadable."""
    try:
        manifest = load_manifest(pack_dir)
    except (SkillPackError, OSError, ValueError):
        return "unknown"
    return manifest.get("package_version", "unknown")


def version_key(version: str) -> tuple[int, ...]:
    """Return a version as a comparable tuple, or () if unparseable."""
    match = re.match(r"\d+(?:\.\d+)*", version.strip())
    if match:
        parts = [int(part) for part in match.group(0).split(".")]
        # Remove trailing zeros from the version number
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return tuple(parts)
    return ()


def pack_shipped_with_nsys(nsys: str) -> Path | None:
    """Return the skill pack bundled with the found nsys install, or None."""
    if not nsys:
        return None
    resolved = Path(nsys).resolve()
    if len(resolved.parents) < 2:
        return None
    pack = resolved.parents[1] / "skills" / SKILL_PACK_NAME
    return pack if _looks_like_skill_pack(pack) else None


def find_newest_pack(
    candidate_skill_packs: list[Path], running_key: tuple[int, ...]
) -> tuple[str, tuple[int, ...]]:
    """Return the newest skill pack version and key."""
    newest_version = ""
    newest_key = running_key

    for candidate in candidate_skill_packs:
        candidate_version = pack_version(candidate)
        candidate_key = version_key(candidate_version)
        if candidate_key > newest_key:
            newest_version = candidate_version
            newest_key = candidate_key
    return newest_version, newest_key


def _skill_path(data_root: Path) -> Path:
    return data_root / PROJECT_DIR_NAME / "skills" / SKILL_PACK_NAME


def _configured_skill_pack_path(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if _looks_like_skill_pack(path):
        return path
    raise SkillPackPathError(
        f"Invalid {label} path: {path}. Expected SKILL.md and skill-pack manifest metadata."
    )


def _looks_like_skill_pack(path: Path) -> bool:
    has_manifest = (path / "manifest.json").is_file() or (
        path / "assets" / "nsight-systems" / "manifest.json"
    ).is_file()
    return has_manifest and (path / "SKILL.md").is_file()


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped
