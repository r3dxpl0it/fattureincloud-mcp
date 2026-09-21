"""Runtime bootstrap: make the bundled dependencies importable, or explain why not.

Why this module exists
----------------------
The MCPB bundle ships its dependencies pre-built in ``lib/``. Some of them
(``pydantic_core``, ``rpds``, ``cffi``, ``cryptography``) are compiled C/Rust
extensions whose filenames are ABI-locked to one CPython minor version, e.g.
``_pydantic_core.cpython-312-darwin.so`` only loads on CPython 3.12.

Claude Desktop launches the server with a bare ``python3``, resolved from the
PATH it happens to spawn with. That can be any version the user has installed.
When it does not match the bundled ABI the very first import raises
``ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'``, the
process dies before it can speak MCP, and the only thing the user sees is:

    Couldn't start for Cowork and Code sessions. Error: Connection closed

That message is unactionable. This module replaces it with a precise
diagnosis, and — when a matching runtime is available anywhere — quietly makes
it work instead.

Resolution order for a given interpreter (ABI tag ``cp314`` for CPython 3.14):

1. ``~/.fattureincloud-mcp/runtime/cp314/``  — user-built, survives bundle
   reinstalls, wins because it was built for exactly this interpreter.
2. ``lib/cp314/`` + ``lib/shared/``          — the multi-ABI bundle layout.
3. ``lib/``                                  — the legacy flat layout. Used
   only when its ABI matches; otherwise we fail with the report below.
4. No ``lib/`` at all                        — development checkout running
   from a virtualenv. Nothing to do.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: Environment variable overriding the user runtime root.
RUNTIME_DIR_ENV = "FIC_RUNTIME_DIR"

#: Set to "1" to downgrade a fatal ABI mismatch to a warning. Escape hatch for
#: users who have the dependencies installed somewhere else entirely.
RUNTIME_LENIENT_ENV = "FIC_RUNTIME_LENIENT"

_SO_ABI_RE = re.compile(r"\.cpython-(\d)(\d+)-")

REPO_URL = "https://github.com/aringad/fattureincloud-mcp"

#: Floor imposed by our dependencies (the mcp SDK requires >= 3.10). Below this
#: no amount of rebuilding helps, so the diagnosis has to say something else.
MIN_PYTHON = (3, 10)


def abi_tag(version_info=None) -> str:
    """ABI tag for an interpreter, e.g. ``cp314`` for CPython 3.14."""
    vi = version_info or sys.version_info
    return f"cp{vi[0]}{vi[1]}"


def bundle_dir() -> Path:
    return Path(__file__).resolve().parent


def user_runtime_root() -> Path:
    override = os.environ.get(RUNTIME_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".fattureincloud-mcp" / "runtime"


def bundled_abis(lib: Path) -> list[str]:
    """ABI tags shipped in a multi-ABI ``lib/`` (``cp312``, ``cp313``, ...)."""
    if not lib.is_dir():
        return []
    return sorted(
        p.name for p in lib.iterdir() if p.is_dir() and re.fullmatch(r"cp\d{3,}", p.name)
    )


def flat_layout_abi(lib: Path, _max_entries: int = 4000) -> str | None:
    """ABI tag of a legacy flat ``lib/``, read off its compiled extensions.

    Returns ``None`` for a pure-Python tree (no ``.so`` at all), which is
    portable and therefore always safe to use.
    """
    if not lib.is_dir():
        return None
    seen = 0
    for path in lib.rglob("*.so"):
        seen += 1
        if seen > _max_entries:  # pathological tree; stop scanning
            break
        match = _SO_ABI_RE.search(path.name)
        if match:
            return f"cp{match.group(1)}{match.group(2)}"
    return None


def resolve(tag: str | None = None) -> dict:
    """Work out which dependency roots to use. Pure: does not touch sys.path."""
    tag = tag or abi_tag()
    lib = bundle_dir() / "lib"
    user_dir = user_runtime_root() / tag

    report: dict = {
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}",
        "executable": sys.executable,
        "abi_tag": tag,
        "bundle": str(bundle_dir()),
        "user_runtime": str(user_dir),
        "bundled_abis": bundled_abis(lib),
        "paths": [],
        "layout": None,
        "ok": True,
        "problem": None,
        "too_old": sys.version_info[:2] < MIN_PYTHON,
    }

    # 1. A user-built runtime for exactly this interpreter always wins.
    if user_dir.is_dir() and any(user_dir.iterdir()):
        report["layout"] = "user"
        report["paths"] = [str(user_dir)]
        shared = lib / "shared"
        if shared.is_dir():
            report["paths"].append(str(shared))
        return report

    # 2. Multi-ABI bundle layout.
    abis = report["bundled_abis"]
    if abis:
        report["layout"] = "multi-abi"
        matching = lib / tag
        shared = lib / "shared"
        if matching.is_dir():
            report["paths"] = [str(matching)]
            if shared.is_dir():
                report["paths"].append(str(shared))
            return report
        report["ok"] = False
        report["problem"] = (
            f"this bundle ships native dependencies for {', '.join(abis)}, "
            f"but you are running CPython {report['python']} ({tag})"
        )
        return report

    # 3. Legacy flat layout.
    if lib.is_dir():
        report["layout"] = "flat"
        found = flat_layout_abi(lib)
        if found is None or found == tag:
            # Pure-Python tree, or compiled for exactly this interpreter.
            report["paths"] = [str(lib)]
            return report
        report["ok"] = False
        report["bundled_abis"] = [found]
        report["problem"] = (
            f"this bundle's lib/ was built for {found}, "
            f"but you are running CPython {report['python']} ({tag})"
        )
        return report

    # 4. No bundled deps: development checkout / virtualenv.
    report["layout"] = "none"
    return report


def _python_minor(tag: str) -> str:
    """``cp312`` -> ``3.12`` for display."""
    match = re.fullmatch(r"cp(\d)(\d+)", tag)
    return f"{match.group(1)}.{match.group(2)}" if match else tag


def format_problem(report: dict) -> str:
    """The message a stuck user actually needs, not a traceback."""
    wanted = report.get("bundled_abis") or []
    versions = ", ".join(_python_minor(a) for a in wanted) or "a different version"
    first = wanted[0] if wanted else abi_tag()
    repair = Path(report["bundle"]) / "scripts" / "repair-runtime.sh"

    header = [
        "",
        "=" * 72,
        "  FattureInCloud MCP - cannot start: Python version mismatch",
        "=" * 72,
        "",
        f"  {report['problem']}.",
        "",
        f"  Running : {report['executable']}",
        f"            CPython {report['python']}  (ABI {report['abi_tag']})",
        f"  Bundled : Python {versions}",
        "",
        "  Compiled dependencies (pydantic-core, rpds, cffi) only load on the",
        "  exact Python minor version they were built for, so the server cannot",
        "  start. Claude Desktop reports this as 'Error: Connection closed'.",
        "",
    ]

    if report.get("too_old"):
        # Rebuilding cannot help: the dependencies themselves require 3.10+.
        body = [
            f"  Python {'.'.join(str(p) for p in MIN_PYTHON)} or newer is required, so there is nothing",
            "  to rebuild here. Install a supported Python and make sure it is the",
            "  'python3' found first on PATH:",
            "",
            f"       brew install python@{_python_minor(first)}    # macOS",
            "",
        ]
    else:
        body = [
            "  Fix it in one of two ways:",
            "",
            f"  1. Build a runtime for the Python you actually have ({report['python']}):",
            "",
            f'       bash "{repair}"',
            "",
            f"     This installs the dependencies into {report['user_runtime']}",
            "     and the server picks them up automatically from then on - it also",
            "     survives reinstalling the extension.",
            "",
            f"  2. Or install Python {_python_minor(first)} and make sure it is the",
            "     'python3' on your PATH before the one above.",
            "",
        ]

    return "\n".join(header + body + [
        f"  Details: {REPO_URL}/blob/main/docs/RUNTIME.md",
        "=" * 72,
        "",
    ])


def bootstrap(*, strict: bool = True) -> dict:
    """Put the right dependency roots on ``sys.path``.

    On mismatch: print an actionable report to stderr and exit non-zero, so the
    failure lands in the Claude Desktop extension log as text a user can act on
    rather than an ImportError traceback. ``FIC_RUNTIME_LENIENT=1`` (or
    ``strict=False``) downgrades that to a warning and continues.
    """
    report = resolve()

    if not report["ok"]:
        sys.stderr.write(format_problem(report))
        sys.stderr.flush()
        if strict and os.environ.get(RUNTIME_LENIENT_ENV) != "1":
            raise SystemExit(78)  # EX_CONFIG
        return report

    for path in reversed(report["paths"]):
        if path not in sys.path:
            sys.path.insert(0, path)
    return report
