"""File-based local cache for static reference data.

Stored as JSON files in a per-company subdirectory under the cache root.
Override root via FIC_CACHE_DIR env var (default: ~/.fattureincloud-mcp/cache/).
Disable entirely via FIC_CACHE_DISABLED=1.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable


def cache_root() -> Path:
    env = os.environ.get("FIC_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".fattureincloud-mcp" / "cache"


def cache_disabled() -> bool:
    return os.environ.get("FIC_CACHE_DISABLED") == "1"


def _cache_path(resource: str, company_id: str | int) -> Path:
    return cache_root() / str(company_id) / f"{resource}.json"


def get(
    resource: str,
    company_id: str | int,
    ttl: timedelta = timedelta(hours=24),
) -> Any | None:
    if cache_disabled():
        return None
    path = _cache_path(resource, company_id)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl.total_seconds():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def put(resource: str, company_id: str | int, value: Any) -> None:
    if cache_disabled():
        return
    path = _cache_path(resource, company_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Cached payloads are client and supplier registries: names, VAT numbers,
    # addresses. Keep them owner-only rather than world-readable 0644.
    _restrict(path.parent, 0o700)

    # Write atomically: a crash or a concurrent writer used to be able to leave
    # a half-written file that every later read discarded as corrupt JSON.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        _restrict(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _restrict(path: Path, mode: int) -> None:
    """Best-effort permission tightening; never fatal (e.g. on a FAT volume)."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def invalidate(resource: str, company_id: str | int) -> None:
    path = _cache_path(resource, company_id)
    if path.exists():
        path.unlink()


def invalidate_all(company_id: str | int) -> None:
    company_dir = cache_root() / str(company_id)
    if company_dir.exists():
        for f in company_dir.glob("*.json"):
            f.unlink()


def cached(resource: str, ttl: timedelta = timedelta(hours=24)):
    """Decorator: cache the result of a function keyed by (resource, company_id).

    The decorated function must accept `company_id` as a keyword argument.
    """
    def decorator(fn: Callable):
        def wrapper(*args, company_id, **kwargs):
            hit = get(resource, company_id, ttl)
            if hit is not None:
                return hit
            value = fn(*args, company_id=company_id, **kwargs)
            put(resource, company_id, value)
            return value
        return wrapper
    return decorator
