"""Runtime loader for the official Nsight Systems skill pack."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class SkillPackError(RuntimeError):
    """Raised when a runtime skill pack is invalid or unsupported."""


@dataclass(frozen=True)
class SkillPack:
    root: Path
    manifest: dict[str, Any]
    docs_index: list[dict[str, Any]]
    recipes_index: list[dict[str, Any]]

    @classmethod
    def load(cls, root: str | Path) -> SkillPack:
        path = Path(root).expanduser().resolve()
        manifest = load_manifest(path)
        _validate(path, manifest)
        resources = manifest["resources"]
        docs_index = json.loads((path / resources["docs_index"]).read_text(encoding="utf-8"))
        recipes_index = json.loads((path / resources["recipes_index"]).read_text(encoding="utf-8"))
        if not isinstance(docs_index, list) or not isinstance(recipes_index, list):
            raise SkillPackError("indexes must be JSON lists")
        return cls(path, manifest, docs_index, recipes_index)

    @property
    def package_version(self) -> str:
        return str(self.manifest.get("package_version", "unknown"))

    @property
    def build_nsys_version(self) -> str:
        return str(self.manifest.get("nsys", {}).get("build_nsys_version", "unknown"))


def load_manifest(path: Path) -> dict[str, Any]:
    """Load the manifest from a skill pack or NV-BASE export."""
    manifest_path = _manifest_path(path)
    if not manifest_path.is_file():
        raise SkillPackError(f"manifest.json not found: {path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _manifest_path(root: Path) -> Path:
    """Return the manifest for a product skill pack or NV-BASE export.

    Product builds keep `manifest.json` in the skill root. NV-BASE only
    considers `references/`, `scripts/`, `assets/`, and `evals/` expected
    top-level entries, so the generated NV-BASE export stores runtime
    metadata under `assets/nsight-systems/`. These are two current artifact
    layouts, not an old-version compatibility path.
    """

    for rel in (
        "manifest.json",
        "assets/nsight-systems/manifest.json",
    ):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return root / "manifest.json"


def _validate(root: Path, manifest: dict[str, Any]) -> None:
    name = manifest.get("name")
    aliases = manifest.get("runtime_aliases", [])
    if name != "nsight-systems":
        raise SkillPackError("manifest name must be nsight-systems")
    if "nsys" not in aliases:
        raise SkillPackError("manifest runtime_aliases must include nsys")
    for key in ("entry", "resources", "scripts"):
        if key not in manifest:
            raise SkillPackError(f"manifest missing {key}")
    _path(root, manifest["entry"], file=True)
    for rel in manifest["resources"].values():
        _path(root, rel, file=None)
    for rel in manifest["scripts"].values():
        _path(root, rel, file=True)


def _path(root: Path, rel: Any, *, file: bool | None) -> Path:
    if not isinstance(rel, str) or not rel:
        raise SkillPackError("manifest path must be a non-empty string")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        raise SkillPackError(f"manifest path escapes pack: {rel}")
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SkillPackError(f"manifest path escapes pack: {rel}") from exc
    if not path.exists():
        raise SkillPackError(f"manifest path not found: {rel}")
    if file is True and not path.is_file():
        raise SkillPackError(f"manifest path is not a file: {rel}")
    if file is False and not path.is_dir():
        raise SkillPackError(f"manifest path is not a directory: {rel}")
    return path
