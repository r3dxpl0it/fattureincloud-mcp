"""Tests for _runtime: the ABI-aware dependency bootstrap.

Regression cover for the failure shipped in 2.0.0. The bundle's compiled
dependencies were built for CPython 3.12; Claude Desktop launched the server
with whatever `python3` resolved to (3.14 on Apple Silicon Homebrew), the first
import raised

    ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'

and the process died before it could speak MCP. All the user ever saw was
"Couldn't start for Cowork and Code sessions. Error: Connection closed".
"""

import sys

import pytest

import _runtime


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """An empty fake bundle, with the user runtime root pointed somewhere safe."""
    monkeypatch.setattr(_runtime, "bundle_dir", lambda: tmp_path)
    monkeypatch.setenv(_runtime.RUNTIME_DIR_ENV, str(tmp_path / "user-runtime"))
    monkeypatch.delenv(_runtime.RUNTIME_LENIENT_ENV, raising=False)
    return tmp_path


def _locked_so(directory, abi):
    """Create a version-locked extension, e.g. _pydantic_core.cpython-312-darwin.so"""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = abi.replace("cp", "cpython-")
    (directory / f"_pydantic_core.{stamp}-darwin.so").touch()


def make_multi_abi(bundle, abis):
    for abi in abis:
        _locked_so(bundle / "lib" / abi / "pydantic_core", abi)
    (bundle / "lib" / "shared").mkdir(parents=True, exist_ok=True)


def make_flat(bundle, abi=None):
    lib = bundle / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "some_pure_package").mkdir(exist_ok=True)
    if abi:
        _locked_so(lib / "pydantic_core", abi)


def foreign_tag():
    """An ABI tag that is never the running interpreter's, on any Python."""
    return f"cp{sys.version_info[0]}{sys.version_info[1] + 1}"


def make_user_runtime(bundle, abi):
    directory = bundle / "user-runtime" / abi
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "marker.py").touch()
    return directory


# --- ABI tagging ---------------------------------------------------------


def test_abi_tag_matches_running_interpreter():
    assert _runtime.abi_tag() == f"cp{sys.version_info[0]}{sys.version_info[1]}"


def test_abi_tag_is_explicit_about_version():
    assert _runtime.abi_tag((3, 12, 7)) == "cp312"
    assert _runtime.abi_tag((3, 9, 6)) == "cp39"


# --- multi-ABI layout ----------------------------------------------------


def test_multi_abi_selects_the_matching_directory(bundle):
    current = _runtime.abi_tag()
    make_multi_abi(bundle, [current, "cp399"])

    report = _runtime.resolve()

    assert report["ok"]
    assert report["layout"] == "multi-abi"
    assert str(bundle / "lib" / current) == report["paths"][0]
    assert str(bundle / "lib" / "shared") in report["paths"]


def test_multi_abi_without_a_matching_directory_fails(bundle):
    make_multi_abi(bundle, ["cp312"])

    report = _runtime.resolve(tag="cp314")

    assert not report["ok"]
    assert "cp312" in report["problem"]
    assert report["bundled_abis"] == ["cp312"]


# --- user runtime (what repair-runtime.sh produces) ----------------------


def test_user_runtime_wins_over_the_bundle(bundle):
    current = _runtime.abi_tag()
    make_multi_abi(bundle, [current])
    user_dir = make_user_runtime(bundle, current)

    report = _runtime.resolve()

    assert report["ok"]
    assert report["layout"] == "user"
    assert report["paths"][0] == str(user_dir)


def test_user_runtime_rescues_an_unsupported_interpreter(bundle):
    """The repaired runtime is why a reinstall of the bundle does not re-break."""
    current = _runtime.abi_tag()
    make_multi_abi(bundle, ["cp312"])  # bundle cannot serve this interpreter
    make_user_runtime(bundle, current)

    report = _runtime.resolve()

    assert report["ok"]
    assert report["layout"] == "user"


def test_empty_user_runtime_is_ignored(bundle):
    current = _runtime.abi_tag()
    make_multi_abi(bundle, [current])
    (bundle / "user-runtime" / current).mkdir(parents=True)

    report = _runtime.resolve()

    assert report["layout"] == "multi-abi"


# --- legacy flat layout (what 2.0.0 shipped) -----------------------------


def test_flat_layout_with_matching_abi_is_used(bundle):
    make_flat(bundle, abi=_runtime.abi_tag())

    report = _runtime.resolve()

    assert report["ok"]
    assert report["layout"] == "flat"
    assert report["paths"] == [str(bundle / "lib")]


def test_flat_layout_with_foreign_abi_is_rejected(bundle):
    """The exact 2.0.0 bug: cp312 wheels under a different interpreter."""
    make_flat(bundle, abi="cp312")

    report = _runtime.resolve(tag="cp314")

    assert not report["ok"]
    assert "cp312" in report["problem"]


def test_pure_python_flat_layout_is_always_portable(bundle):
    make_flat(bundle, abi=None)

    report = _runtime.resolve(tag="cp399")

    assert report["ok"]


def test_abi3_extensions_are_not_version_locked(bundle):
    """cryptography ships _rust.abi3.so, which loads on any Python 3.x."""
    lib = bundle / "lib" / "cryptography" / "hazmat" / "bindings"
    lib.mkdir(parents=True)
    (lib / "_rust.abi3.so").touch()

    assert _runtime.flat_layout_abi(bundle / "lib") is None
    assert _runtime.resolve(tag="cp314")["ok"]


def test_no_lib_directory_is_a_noop(bundle):
    """Development checkout: dependencies come from the virtualenv."""
    report = _runtime.resolve()

    assert report["ok"]
    assert report["layout"] == "none"
    assert report["paths"] == []


# --- bootstrap behaviour -------------------------------------------------


def test_bootstrap_inserts_paths_ahead_of_sys_path(bundle, monkeypatch):
    current = _runtime.abi_tag()
    make_multi_abi(bundle, [current])
    monkeypatch.setattr(sys, "path", list(sys.path))

    _runtime.bootstrap()

    assert sys.path[0] == str(bundle / "lib" / current)
    assert sys.path[1] == str(bundle / "lib" / "shared")


def test_bootstrap_exits_with_config_code_on_mismatch(bundle, capsys):
    # A flat lib/ built for an ABI that is never the running interpreter's.
    make_flat(bundle, abi=foreign_tag())

    with pytest.raises(SystemExit) as excinfo:
        _runtime.bootstrap()

    assert excinfo.value.code == 78  # EX_CONFIG
    assert "Connection closed" in capsys.readouterr().err


def test_lenient_mode_warns_but_continues(bundle, monkeypatch, capsys):
    make_flat(bundle, abi=foreign_tag())
    monkeypatch.setenv(_runtime.RUNTIME_LENIENT_ENV, "1")

    report = _runtime.bootstrap()

    assert not report["ok"]
    assert "cannot start" in capsys.readouterr().err


# --- the message a stuck user reads --------------------------------------


def test_problem_report_points_at_the_repair_script(bundle):
    make_multi_abi(bundle, ["cp312"])
    report = _runtime.resolve(tag="cp314")

    text = _runtime.format_problem(report)

    assert "repair-runtime.sh" in text
    assert str(bundle) in text
    assert "Connection closed" in text
    assert "3.12" in text


def test_report_does_not_suggest_rebuilding_below_the_floor(bundle):
    make_multi_abi(bundle, ["cp312"])
    report = _runtime.resolve(tag="cp39")
    report["too_old"] = True

    text = _runtime.format_problem(report)

    assert "or newer is required" in text
    assert "repair-runtime.sh" not in text
