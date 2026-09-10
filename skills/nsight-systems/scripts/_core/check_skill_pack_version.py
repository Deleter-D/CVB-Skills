#!/usr/bin/env python3
"""Warn when a newer nsight-systems skill pack is installed than the one running."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap_core import ensure_core  # noqa: E402

ensure_core()

from nsys_agent_tools.skill_pack_paths import (  # noqa: E402
    agent_installed_packs,
    find_newest_pack,
    pack_shipped_with_nsys,
    pack_version,
    version_key,
)


def main() -> int:
    # Find the running skill pack
    running_pack = Path(__file__).resolve().parents[2]
    running_version = pack_version(running_pack)
    running_key = version_key(running_version)
    if not running_key:
        return 0

    candidate_skill_packs: list[Path] = []

    # Find skill pack shipped with the found nsys
    if nsys_pack := pack_shipped_with_nsys(os.environ.get("NSYS_PATH", "")):
        candidate_skill_packs.append(nsys_pack)

    # Find skill packs installed under agents' skill roots
    candidate_skill_packs.extend(agent_installed_packs())

    newest_version, newest_key = find_newest_pack(candidate_skill_packs, running_key)

    if newest_key > running_key:
        print(f"{running_version} {newest_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
