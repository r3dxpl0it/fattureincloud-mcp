#!/usr/bin/env bash
# Build the MCPB bundle for Claude Desktop one-click install.
#
# Usage:
#   ./scripts/build.sh                          # build for the default ABIs
#   TARGET_PYTHONS="3.12 3.13" ./scripts/build.sh
#   SKIP_PACK=1 ./scripts/build.sh              # build lib/ only, don't pack
#
# Claude Desktop launches the server with a bare `python3`, resolved from
# whatever PATH it spawns with - which is not under our control and differs per
# machine. Compiled dependencies are locked to one CPython minor version, so a
# bundle built for a single version dies on import for everyone else, and the
# app can only report "Error: Connection closed".
#
# This builds every supported ABI and lets _runtime.py select at startup:
#
#   lib/shared/   portable packages, installed once (incl. abi3 extensions)
#   lib/cp312/    only the version-locked packages, per CPython minor
#   lib/cp313/
#   ...
#
# Versions come from requirements.lock so a rebuild is reproducible. uv resolves
# wheels for Python versions that are not installed locally, so a single machine
# can build every ABI.
set -euo pipefail

cd "$(dirname "$0")/.."

TARGET_PYTHONS="${TARGET_PYTHONS:-3.11 3.12 3.13 3.14}"
LOCK="requirements.lock"

if [[ ! -f "$LOCK" ]]; then
  echo "ERROR: $LOCK not found. Regenerate it with:" >&2
  echo "  uv pip compile requirements.txt --output-file requirements.lock" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' not found. Install with:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

BUILD_PYTHON="${BUILD_PYTHON:-$(command -v python3)}"

echo "==> Cleaning previous build artifacts"
rm -rf lib/ dist/ .build-staging/
mkdir -p lib dist

for version in $TARGET_PYTHONS; do
  abi="cp${version//./}"
  staging=".build-staging/$abi"
  echo "==> Resolving dependencies for Python $version ($abi)"
  rm -rf "$staging"
  mkdir -p "$staging"

  # --only-binary: never compile from source. A source build would silently
  # produce an extension for the *building* interpreter, not the target.
  uv pip install \
    --python-version "$version" \
    --only-binary=:all: \
    --target "$staging" \
    --quiet \
    -r "$LOCK"

  "$BUILD_PYTHON" scripts/_split_abi.py "$staging" "$abi" lib
done

rm -rf .build-staging/

echo "==> Verifying ABI layout"
"$BUILD_PYTHON" - <<'PY'
import re
import sys
from pathlib import Path

lib = Path("lib")
locked_re = re.compile(r"\.(?:cpython-3(\d+)|cp3(\d+))-[^.]*\.(?:so|pyd)$")
problems = []

abis = sorted(p.name for p in lib.iterdir() if p.is_dir() and p.name.startswith("cp"))
if not abis:
    problems.append("no ABI directories were produced")

for abi in abis:
    expected = abi[2:]  # cp312 -> 312
    found = list((lib / abi).rglob("*.so")) + list((lib / abi).rglob("*.pyd"))
    if not found:
        problems.append(f"{abi}: no compiled extensions - nothing to pin")
    for ext in found:
        match = locked_re.search(ext.name)
        if not match:
            problems.append(f"{abi}: {ext.name} is not version-locked; it belongs in shared/")
            continue
        tag = f"3{match.group(1) or match.group(2)}"
        if tag != expected:
            problems.append(f"{abi}: {ext.name} is built for {tag}, not {expected}")

# Nothing version-locked may hide in shared/: it would load for one interpreter
# and break every other one.
for ext in list((lib / "shared").rglob("*.so")) + list((lib / "shared").rglob("*.pyd")):
    if locked_re.search(ext.name):
        problems.append(f"shared/: {ext.name} is version-locked and must live under an ABI dir")

if problems:
    print("    FAILED:")
    for problem in problems:
        print(f"      - {problem}")
    sys.exit(1)

print(f"    OK: {', '.join(abis)} + shared")
PY

echo "==> lib/ sizes"
du -sh lib/* | sort -h | sed 's/^/    /'
echo "    total: $(du -sh lib | cut -f1)"

if [[ "${SKIP_PACK:-0}" == "1" ]]; then
  echo "==> SKIP_PACK=1, not packing"
  exit 0
fi

if ! command -v mcpb >/dev/null 2>&1; then
  echo "ERROR: 'mcpb' CLI not found. Install with:" >&2
  echo "  npm install -g @anthropic-ai/mcpb" >&2
  exit 1
fi

echo "==> Packing MCPB"
mcpb pack . dist/fattureincloud.mcpb

echo
echo "==> Bundle ready"
ls -lh dist/fattureincloud.mcpb
