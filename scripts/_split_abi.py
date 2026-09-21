#!/usr/bin/env python3
"""Split a `pip install --target` tree into ABI-locked and portable halves.

Only a handful of distributions ship extensions tied to one CPython minor
version (`_pydantic_core.cpython-312-darwin.so`). Everything else - including
`cryptography`, which builds against the stable `abi3` ABI - loads on any
supported interpreter.

So instead of shipping a whole dependency tree per Python version, the bundle
ships each portable distribution once in ``lib/shared/`` and only the locked
ones per ABI in ``lib/cp312/``, ``lib/cp313/``, ... That keeps a four-version
bundle roughly the size of the old single-version one.

Usage:
    _split_abi.py <staging-dir> <abi-tag> <lib-dir>
"""

from __future__ import annotations

import csv
import re
import shutil
import sys
from pathlib import Path

#: Extensions named for one CPython minor version, e.g. `.cpython-312-darwin.so`
#: or `.cp312-win_amd64.pyd`. Stable-ABI files (`.abi3.so`) deliberately do not
#: match: they are portable.
LOCKED_EXT_RE = re.compile(r"\.(?:cpython-3\d+|cp3\d+)-[^.]*\.(?:so|pyd)$")


def distributions(staging: Path):
    """Yield (dist_info_dir, top_level_names) for each installed distribution."""
    for dist_info in sorted(staging.glob("*.dist-info")):
        record = dist_info / "RECORD"
        tops: set[str] = {dist_info.name}
        files: list[str] = []
        if record.is_file():
            with record.open(newline="", encoding="utf-8") as handle:
                for row in csv.reader(handle):
                    if not row or not row[0]:
                        continue
                    name = row[0]
                    files.append(name)
                    top = name.split("/", 1)[0]
                    # Ignore escapes and bin/ shims; we only ship importables.
                    if top not in ("..", ".") and not top.endswith(".data"):
                        tops.add(top)
        yield dist_info, sorted(tops), files


def is_locked(files: list[str]) -> bool:
    return any(LOCKED_EXT_RE.search(name) for name in files)


def move(src: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        # A later ABI re-supplying an identical portable package: keep the
        # first copy, they resolve from the same lock file.
        shutil.rmtree(src) if src.is_dir() else src.unlink()
        return
    shutil.move(str(src), str(dest))


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2

    staging, abi, lib = Path(argv[1]), argv[2], Path(argv[3])
    if not staging.is_dir():
        print(f"ERROR: staging dir not found: {staging}", file=sys.stderr)
        return 1

    abi_dir = lib / abi
    shared_dir = lib / "shared"

    locked_names: list[str] = []
    portable_names: list[str] = []

    for dist_info, tops, files in distributions(staging):
        target = abi_dir if is_locked(files) else shared_dir
        (locked_names if target is abi_dir else portable_names).append(
            dist_info.name.split("-")[0]
        )
        for top in tops:
            src = staging / top
            if src.exists():
                move(src, target)

    # Anything pip dropped that no RECORD claimed (rare: stray .pth, bin shims).
    leftovers = [p for p in staging.iterdir()]
    for leftover in leftovers:
        if leftover.name in ("bin", "__pycache__") or leftover.suffix == ".pth":
            shutil.rmtree(leftover, ignore_errors=True) if leftover.is_dir() else leftover.unlink()
        else:
            move(leftover, shared_dir)

    print(f"    {abi}: {len(locked_names)} ABI-locked ({', '.join(sorted(locked_names)) or '-'})")
    print(f"    shared: {len(portable_names)} portable packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
