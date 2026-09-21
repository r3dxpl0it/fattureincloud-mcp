"""Startup configuration handling and calendar arithmetic.

Two classes of bug covered here, both of which used to surface to the user as
something other than what they were:

* ``int(os.getenv("FIC_COMPANY_ID", "0"))`` raised ValueError at import when the
  extension's Company ID field was left blank, killing the server before it
  could explain itself - Claude Desktop showed only "Error: Connection closed".
* February was hardcoded to 29 days, so a monthly filter in a non-leap year
  asked the API for documents dated up to an impossible 2025-02-29.
"""

import asyncio
import importlib
import json
import sys

import pytest


def import_server(monkeypatch, **env):
    """Import a fresh `server` module under a specific environment."""
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    for mod in ("server", "cache"):
        sys.modules.pop(mod, None)
    return importlib.import_module("server")


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("FIC_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("FIC_CACHE_DISABLED", raising=False)
    return import_server(
        monkeypatch,
        FIC_ACCESS_TOKEN="a/test-token",
        FIC_COMPANY_ID="100",
        FIC_SENDER_EMAIL="billing@example.invalid",
    )


def _run(coro):
    return asyncio.run(coro)


# --- configuration -------------------------------------------------------


def test_valid_configuration_reports_no_problems(configured):
    assert configured.CONFIG_ERRORS == []
    assert configured.COMPANY_ID == 100
    assert configured.ACCESS_TOKEN == "a/test-token"


def test_blank_company_id_does_not_raise(tmp_path, monkeypatch):
    """The manifest substitutes an empty string when the field is left blank."""
    monkeypatch.setenv("FIC_CACHE_DIR", str(tmp_path))
    server = import_server(
        monkeypatch, FIC_ACCESS_TOKEN="a/tok", FIC_COMPANY_ID="", FIC_SENDER_EMAIL=None
    )

    assert server.COMPANY_ID == 0
    assert any("FIC_COMPANY_ID" in problem for problem in server.CONFIG_ERRORS)


def test_missing_company_id_variable_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("FIC_CACHE_DIR", str(tmp_path))
    server = import_server(
        monkeypatch, FIC_ACCESS_TOKEN="a/tok", FIC_COMPANY_ID=None, FIC_SENDER_EMAIL=None
    )

    assert server.COMPANY_ID == 0
    assert server.CONFIG_ERRORS


def test_non_numeric_company_id_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("FIC_CACHE_DIR", str(tmp_path))
    server = import_server(
        monkeypatch, FIC_ACCESS_TOKEN="a/tok", FIC_COMPANY_ID="my-company", FIC_SENDER_EMAIL=None
    )

    assert server.COMPANY_ID == 0
    assert any("non valido" in problem for problem in server.CONFIG_ERRORS)


def test_whitespace_is_tolerated(tmp_path, monkeypatch):
    monkeypatch.setenv("FIC_CACHE_DIR", str(tmp_path))
    server = import_server(
        monkeypatch, FIC_ACCESS_TOKEN="  a/tok  ", FIC_COMPANY_ID="  100  ", FIC_SENDER_EMAIL=None
    )

    assert server.COMPANY_ID == 100
    assert server.ACCESS_TOKEN == "a/tok"
    assert server.CONFIG_ERRORS == []


def test_missing_token_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("FIC_CACHE_DIR", str(tmp_path))
    server = import_server(
        monkeypatch, FIC_ACCESS_TOKEN="", FIC_COMPANY_ID="100", FIC_SENDER_EMAIL=None
    )

    assert any("FIC_ACCESS_TOKEN" in problem for problem in server.CONFIG_ERRORS)


def test_tools_explain_misconfiguration_instead_of_failing_obscurely(tmp_path, monkeypatch):
    monkeypatch.setenv("FIC_CACHE_DIR", str(tmp_path))
    server = import_server(
        monkeypatch, FIC_ACCESS_TOKEN="", FIC_COMPANY_ID="", FIC_SENDER_EMAIL=None
    )

    result = _run(server.call_tool("list_clients", {}))
    text = result[0].text

    assert "non configurata correttamente" in text
    assert "FIC_ACCESS_TOKEN" in text
    assert "FIC_COMPANY_ID" in text


def test_selfcheck_reports_not_ready_when_unconfigured(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FIC_CACHE_DIR", str(tmp_path))
    server = import_server(
        monkeypatch, FIC_ACCESS_TOKEN="", FIC_COMPANY_ID="", FIC_SENDER_EMAIL=None
    )

    code = server.selfcheck()

    assert code == 1
    assert "NOT ready" in capsys.readouterr().out


def test_selfcheck_never_prints_the_token(configured, capsys):
    configured.selfcheck()

    assert "a/test-token" not in capsys.readouterr().out


# --- calendar arithmetic -------------------------------------------------


@pytest.mark.parametrize(
    "year,month,expected_end",
    [
        (2025, 2, "2025-02-28"),   # non-leap February: was 2025-02-29
        (2024, 2, "2024-02-29"),   # leap February
        (2000, 2, "2000-02-29"),   # divisible by 400
        (1900, 2, "1900-02-28"),   # divisible by 100 but not 400
        (2026, 1, "2026-01-31"),
        (2026, 4, "2026-04-30"),
        (2026, 12, "2026-12-31"),
    ],
)
def test_month_bounds(configured, year, month, expected_end):
    start, end = configured.month_bounds(year, month)

    assert start == f"{year}-{month:02d}-01"
    assert end == expected_end


@pytest.mark.parametrize("month", [0, 13, -1, 99])
def test_month_bounds_rejects_impossible_months(configured, month):
    with pytest.raises(ValueError):
        configured.month_bounds(2026, month)


def test_list_invoices_february_query_is_a_real_date(configured):
    """Regression: the filter used to request documents up to 2025-02-29."""
    from unittest.mock import MagicMock, patch

    response = MagicMock()
    response.data = []

    with patch.object(configured.issued_api, "list_issued_documents", return_value=response) as m:
        _run(configured.call_tool("list_invoices", {"year": 2025, "month": 2}))

    assert "2025-02-28" in m.call_args.kwargs["q"]
    assert "2025-02-29" not in m.call_args.kwargs["q"]


def test_list_invoices_defaults_to_the_current_year(configured):
    """The default was pinned to 2024 while every other tool used today's year."""
    from datetime import datetime
    from unittest.mock import MagicMock, patch

    response = MagicMock()
    response.data = []

    with patch.object(configured.issued_api, "list_issued_documents", return_value=response) as m:
        _run(configured.call_tool("list_invoices", {}))

    assert str(datetime.now().year) in m.call_args.kwargs["q"]


def test_tool_errors_are_concise_by_default(configured, monkeypatch):
    """Tracebacks belong in the extension log, not in the model's context."""
    from unittest.mock import patch

    monkeypatch.delenv("FIC_DEBUG", raising=False)
    with patch.object(
        configured.issued_api, "list_issued_documents", side_effect=RuntimeError("boom")
    ):
        result = _run(configured.call_tool("list_invoices", {"year": 2026}))

    text = result[0].text
    assert "boom" in text
    assert "Traceback" not in text


def test_tool_errors_include_traceback_under_fic_debug(configured, monkeypatch):
    from unittest.mock import patch

    monkeypatch.setenv("FIC_DEBUG", "1")
    with patch.object(
        configured.issued_api, "list_issued_documents", side_effect=RuntimeError("boom")
    ):
        result = _run(configured.call_tool("list_invoices", {"year": 2026}))

    assert "Traceback" in result[0].text
