"""
Parsing what people type and printing what they want to read.

Sizes are the interesting part. ``--memory 2G``, ``--memory 2048M`` and
``--memory 2048`` all mean the same thing, and a CLI that accepted only the last
would be the one tool in the workflow that does. The API speaks integers
(``memory_mb``, ``disk_gb``), so the conversion happens here, once, and a
malformed value is refused with the accepted spellings rather than a stack
trace from ``int()``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from kurukuru.cli.errors import CliError, ExitCode

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*$", re.IGNORECASE)

_MULTIPLIER_MB = {
    "": 1,               # bare number: MB, matching the API field name
    "k": 1 / 1024,
    "m": 1,
    "g": 1024,
    "t": 1024 * 1024,
}


def _parse(value: str, *, unit_label: str) -> tuple[float, str]:
    match = _SIZE.match(value)
    if not match:
        raise CliError(
            f"'{value}' is not a size.",
            ExitCode.USAGE,
            hint=f"Use a number with an optional unit: 2G, 2048M, or a plain "
            f"number meaning {unit_label}.",
        )
    return float(match.group(1)), match.group(2).lower()


def parse_memory_mb(value: str) -> int:
    """``2G`` / ``2048M`` / ``2048`` -> 2048. A bare number is megabytes."""
    amount, unit = _parse(value, unit_label="MB")
    megabytes = amount * _MULTIPLIER_MB[unit]
    if megabytes < 1:
        raise CliError(f"'{value}' is smaller than a megabyte.", ExitCode.USAGE)
    return int(round(megabytes))


def parse_disk_gb(value: str) -> int:
    """``20G`` / ``20480M`` / ``20`` -> 20. A bare number is gigabytes.

    Rounds *up*: the API takes whole gigabytes, and quietly handing someone a
    smaller disk than they asked for is the wrong way to lose a fraction.
    """
    amount, unit = _parse(value, unit_label="GB")
    # A bare number means GB here, unlike memory — which is why the two parsers
    # exist separately rather than sharing a default.
    gigabytes = amount if unit == "" else (amount * _MULTIPLIER_MB[unit]) / 1024
    if gigabytes <= 0:
        raise CliError(f"'{value}' is not a usable disk size.", ExitCode.USAGE)
    return max(1, int(-(-gigabytes // 1)))  # ceil without importing math


def format_memory(megabytes: int | None) -> str:
    """1024 -> ``1G``; 1536 -> ``1.5G``; 512 -> ``512M``.

    Zero is a number, not an absence: ``capacity`` shows 0 MB committed on an
    idle host, and rendering that as an em dash would read as "unknown".
    """
    if megabytes is None:
        return "—"
    if megabytes < 1024:
        return f"{megabytes}M"
    gigabytes = megabytes / 1024
    return f"{gigabytes:.0f}G" if gigabytes.is_integer() else f"{gigabytes:.1f}G"


def format_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover - unreachable, loop always returns


def parse_timestamp(value: str | None) -> datetime | None:
    """Read an API timestamp, treating a naive one as UTC.

    SQLite hands back naive datetimes for rows written before the column was
    timezone-aware, so ``fromisoformat`` alone would produce a value that can't
    be subtracted from ``now()``.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def relative_age(value: str | None) -> str:
    """``4m``, ``3h``, ``2d`` — how long ago, compactly."""
    moment = parse_timestamp(value)
    if moment is None:
        return "—"
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def absolute_time(value: str | None) -> str:
    moment = parse_timestamp(value)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if moment else "—"


#: Colour per instance status, shared by ``ls`` and ``show`` so a status never
#: reads differently in two places.
STATUS_STYLES = {
    "Running": "green",
    "Pending": "yellow",
    "Provisioning": "yellow",
    "Stopped": "blue",
    "Error": "red",
    "Terminated": "dim",
    "Degraded": "dark_orange",
}


def status_text(instance: dict) -> str:
    """Rich markup for an instance's status.

    ``degraded`` is a derived field, not a stored status: a Running instance
    that never published an address. It is shown *instead of* Running because a
    green Running on something you cannot reach is the exact lie the field was
    added to stop.
    """
    label = "Degraded" if instance.get("degraded") else str(instance.get("status", "?"))
    return f"[{STATUS_STYLES.get(label, 'white')}]{label}[/]"
