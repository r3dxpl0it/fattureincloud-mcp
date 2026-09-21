# Runtime and Python compatibility

## The problem this solves

Claude Desktop starts the extension with a bare `python3`, resolved from the
PATH it happens to spawn with. The manifest cannot pin an interpreter, so the
version that actually runs the server is whatever that machine has first on
PATH — 3.11, 3.12, 3.13, 3.14, or the ancient 3.9 that ships with macOS.

Most of the dependency tree is pure Python and does not care. Three packages do:

| Package         | Compiled artifact                        | Portable? |
| --------------- | ---------------------------------------- | --------- |
| `pydantic-core` | `_pydantic_core.cpython-312-darwin.so`   | no        |
| `rpds-py`       | `rpds.cpython-312-darwin.so`             | no        |
| `cffi`          | `_cffi_backend.cpython-312-darwin.so`    | no        |
| `cryptography`  | `_rust.abi3.so`                          | yes (stable ABI) |

The filename carries the CPython minor version. A `cpython-312` extension is
invisible to CPython 3.14 — not an error, simply not found — so the import
fails with:

```
ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'
```

The process then dies before it has spoken a single byte of MCP, and the only
thing the user sees is:

```
Couldn't start for Cowork and Code sessions. Error: Connection closed
```

Which says nothing about Python, versions, or what to do next.

Up to and including v2.0.0 the bundle shipped exactly one ABI (3.12) while
`manifest.json` advertised `"python": ">=3.10"`. On any machine whose `python3`
was not 3.12, the extension could not start.

## How the bundle is laid out now

`scripts/build.sh` resolves the locked dependency set once per supported Python
and splits the result by portability:

```
lib/
  shared/     every portable package, installed once  (~25 MB)
  cp311/      only the version-locked packages         (~6 MB)
  cp312/
  cp313/
  cp314/
```

Because only three packages are ABI-locked, covering four interpreters costs
about as much as the old single-version bundle.

## How the interpreter is selected

`_runtime.bootstrap()` runs before any third-party import and picks, in order:

1. **`~/.fattureincloud-mcp/runtime/<abi>/`** — a runtime built locally by
   `scripts/repair-runtime.sh`. It wins because it was built for exactly the
   interpreter in use, and it survives reinstalling the extension.
2. **`lib/<abi>/` + `lib/shared/`** — the bundled multi-ABI layout.
3. **`lib/`** — the legacy flat layout, used only when its ABI matches (or when
   it contains no compiled extensions at all).
4. **nothing** — a development checkout, where dependencies come from a
   virtualenv. The bootstrap is a no-op.

If none of those can serve the running interpreter, the server prints a report
naming the interpreter, the bundled ABIs and the exact command to fix it, then
exits `78` (`EX_CONFIG`). The report lands in the Claude Desktop extension log,
so "Connection closed" now has something readable behind it.

## Fixing a mismatch

```bash
bash scripts/repair-runtime.sh
```

Installs the locked dependencies for your current `python3` into
`~/.fattureincloud-mcp/runtime/<abi>/`. Restart Claude Desktop afterwards.

To build for a specific interpreter instead:

```bash
TARGET_PYTHON=/opt/homebrew/bin/python3.13 bash scripts/repair-runtime.sh
```

## Diagnosing

```bash
python3 server.py --selfcheck
```

Prints the interpreter, the resolved import paths, whether each critical
dependency imports, and whether the token and company ID are configured. Exit
code `0` means the server is ready to run.

## Escape hatch

`FIC_RUNTIME_LENIENT=1` downgrades a fatal mismatch to a warning and lets the
server continue, for when the dependencies are installed somewhere the
bootstrap does not know about (a system site-packages, a wrapper venv).

## Adding a Python version

```bash
TARGET_PYTHONS="3.11 3.12 3.13 3.14 3.15" ./scripts/build.sh
```

`uv` resolves wheels for interpreters that are not installed locally, so one
machine can build every ABI. The build fails if a target has no wheel for a
locked dependency, rather than silently producing a bundle that cannot start —
which is the failure mode this whole document exists to prevent.

Keep `compatibility.runtimes.python` in `manifest.json` in step with whatever
is actually shipped.
