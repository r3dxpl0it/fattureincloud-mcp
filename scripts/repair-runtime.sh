#!/usr/bin/env bash
# Build the server's dependencies for the Python you actually have.
#
# The MCPB bundle ships compiled dependencies (pydantic-core, rpds, cffi) that
# only load on the exact CPython minor version they were built for. If Claude
# Desktop launches the server with a different python3, it cannot start and
# reports "Error: Connection closed".
#
# This script installs the dependencies into a per-ABI directory under
# ~/.fattureincloud-mcp/runtime/, which the server prefers over its own bundled
# copy. It survives reinstalling the extension.
#
# Usage:
#   bash scripts/repair-runtime.sh                 # repair for `python3`
#   TARGET_PYTHON=/opt/homebrew/bin/python3.13 bash scripts/repair-runtime.sh
#   FIC_RUNTIME_DIR=/tmp/rt bash scripts/repair-runtime.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

TARGET_PYTHON="${TARGET_PYTHON:-$(command -v python3 || true)}"
if [[ -z "$TARGET_PYTHON" ]]; then
  echo "ERROR: no python3 found on PATH. Install Python 3.10+ and re-run." >&2
  exit 1
fi

if ! "$TARGET_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "ERROR: $TARGET_PYTHON is $("$TARGET_PYTHON" -V 2>&1), but this server needs Python 3.10+." >&2
  exit 1
fi

ABI=$("$TARGET_PYTHON" -c 'import sys; print(f"cp{sys.version_info[0]}{sys.version_info[1]}")')
RUNTIME_ROOT="${FIC_RUNTIME_DIR:-$HOME/.fattureincloud-mcp/runtime}"
DEST="$RUNTIME_ROOT/$ABI"

LOCK="$ROOT/requirements.lock"
if [[ ! -f "$LOCK" ]]; then
  LOCK="$ROOT/requirements.txt"
fi
if [[ ! -f "$LOCK" ]]; then
  echo "ERROR: no requirements.lock or requirements.txt next to $ROOT." >&2
  exit 1
fi

echo "==> Target Python : $TARGET_PYTHON ($("$TARGET_PYTHON" -V 2>&1), ABI $ABI)"
echo "==> Requirements  : $LOCK"
echo "==> Installing to : $DEST"

rm -rf "$DEST"
mkdir -p "$DEST"

if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$TARGET_PYTHON" --target "$DEST" --quiet -r "$LOCK"
else
  "$TARGET_PYTHON" -m pip install --target "$DEST" --quiet --no-cache-dir -r "$LOCK"
fi

echo "==> Verifying"
if PYTHONPATH="$DEST" "$TARGET_PYTHON" -c '
import pydantic_core, mcp, fattureincloud_python_sdk  # noqa: F401
print("    imports OK")
'; then
  echo
  echo "==> Runtime repaired for $ABI."
  echo "    Restart Claude Desktop (or toggle the extension off and on)."
else
  echo "ERROR: dependencies installed but still not importable. Please report this at" >&2
  echo "       https://github.com/aringad/fattureincloud-mcp/issues" >&2
  exit 1
fi
