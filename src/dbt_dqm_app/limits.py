"""One serialization/size contract for warehouse downloads and the SQLite issue cache."""

import json
from typing import Any

from .errors import AppError

MAX_CACHE_ISSUES = 50_000
MAX_CACHE_MIB = 128


class CacheLimitExceeded(AppError):
    """A complete snapshot cannot safely replace the current cache."""


def validate_limits(issues: int, mib: int) -> None:
    for name, value, maximum in (("max-cache-issues", issues, MAX_CACHE_ISSUES),
                                 ("max-cache-mib", mib, MAX_CACHE_MIB)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"--{name} must be an integer from 1 to {maximum}")


def issue_json(row: dict[str, Any]) -> str:
    return json.dumps(row, default=str, ensure_ascii=False, separators=(",", ":"))


def check_cache_size(rows: int, size: int, max_issues: int, max_bytes: int) -> None:
    if rows > max_issues or size > max_bytes:
        raise CacheLimitExceeded(
            f"Issue cache limit exceeded: {rows:,} issues (limit {max_issues:,}), "
            f"{size:,} serialized bytes (limit {max_bytes:,}). "
            "Cached data and pending edits are kept. Reduce --archive-cache-days "
            "or capture fewer context columns, then sync again."
        )
