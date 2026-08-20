"""Per-turn time-context injection, ported from @deepseek-ai/dsh-time-context.

dsh's time-context plugin samples the current clock before each model step and
prepends a durable, source-attributed time reading to the request history. The
original is a Cordis plugin for the dsh-agent runtime; here we reuse the exact
rendering logic as plain Python functions so Hermes WebUI can inject the same
block into every user message it forwards to the model.

The format is intentionally byte-compatible with dsh so transcripts look
identical across both harnesses:

    Time sampled while preparing turn 8, step 1: 2026-08-20T20:04:31+08:00[Asia/Singapore]
    Browser time zone for this request: Asia/Singapore. Interpret otherwise-unqualified dates and times in this zone.
    Elapsed since the preceding model-visible message: 10m 28s.
"""

from __future__ import annotations

import re
import time
from contextvars import ContextVar
from datetime import datetime
from typing import Optional

_IANA_TIME_ZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9_+.-]+)+$")

# Per-request client timezone, set at the HTTP boundary (handle_post) from the
# X-Client-Timezone header and read wherever we build the user message. Mirrors
# dsh's browser-time-zone derivation without coupling to its Cordis runtime.
client_timezone_var: ContextVar[Optional[str]] = ContextVar("client_timezone", default=None)


def set_client_timezone(zone: Optional[str]) -> None:
    """Store the canonical client timezone for the current request context."""
    client_timezone_var.set(zone if _validate_time_zone(zone) else None)


def get_client_timezone() -> Optional[str]:
    """Return the client timezone for the current request, or None."""
    return client_timezone_var.get()


def _format_timestamp(now_ms: float, time_zone: str) -> str:
    """Format epoch-ms as an ISO-shaped timestamp with offset and IANA zone.

    Mirrors dsh's createTimestampFormatter + formatTimestamp: 24-hour,
    zero-padded, with a long GMT offset and the canonical zone in brackets.
    """
    dt = datetime.fromtimestamp(now_ms / 1000.0)
    try:
        offset = dt.astimezone().strftime("%z")  # e.g. +0800
        offset_colon = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"
    except Exception:
        offset_colon = "+00:00"
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
        f"{offset_colon}[{time_zone}]"
    )


def _validate_time_zone(value: str | None) -> str | None:
    """Return a canonical IANA zone string, or None if invalid.

    Accepts 'UTC' or a canonical IANA Area/Location. Returns None for anything
    unparseable so the caller can fall back to the server process zone.
    """
    if not value or value == "UTC":
        return None
    if not _IANA_TIME_ZONE.match(value):
        return None
    try:
        # Resolve through Intl-equivalent: Python zoneinfo validates the zone.
        from zoneinfo import ZoneInfo

        ZoneInfo(value)
        return value
    except Exception:
        return None


def render_browser_timezone_context(zone: str | None) -> str:
    """Render the durable browser-zone policy line (dsh-compatible)."""
    if zone:
        return (
            f"Browser time zone for this request: {zone}. "
            "Interpret otherwise-unqualified dates and times in this zone."
        )
    return (
        "Browser time zone for this request: unavailable. "
        "Ask the user to clarify otherwise-unqualified dates and times."
    )


def format_duration(elapsed_ms: float | None) -> str:
    """Format a non-negative elapsed millisecond count as compact units."""
    if elapsed_ms is None:
        return "unavailable"
    total = max(0, int(elapsed_ms // 1000))
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, seconds = divmod(total, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def render_time_context(
    *,
    turn: int,
    step: int,
    now_ms: float,
    previous_ms: float | None,
    time_zone: str,
) -> str:
    """Build the full time-context block for one prepared message.

    Args:
        turn: 1-based conversation turn counter.
        step: 1-based step counter within the turn.
        now_ms: epoch milliseconds sampled at preparation time.
        previous_ms: epoch ms of the preceding model-visible message, or None.
        time_zone: canonical IANA zone to display.
    """
    elapsed = format_duration(None if previous_ms is None else (now_ms - previous_ms))
    baseline = "model-visible message" if step == 1 else "step context"
    browser_text = render_browser_timezone_context(time_zone)
    return (
        f"Time sampled while preparing turn {turn}, step {step}: "
        f"{_format_timestamp(now_ms, time_zone)}\n"
        f"{browser_text}\n"
        f"Elapsed since the preceding {baseline}: {elapsed}."
    )


# ------- WebUI integration helpers -------

# Server-side fallback zone: the process local timezone. WebUI reads the
# client's browser zone from the X-Client-Timezone header and passes it in,
# matching dsh's browser-time-zone derivation.
def server_default_time_zone() -> str:
    """Return the server process IANA zone, falling back to a best-effort name."""
    try:
        from zoneinfo import ZoneInfo

        key = ZoneInfo("localtime").key
        if key and key != "localtime":
            return key
    except Exception:
        pass
    # zoneinfo could not resolve a concrete IANA name (common in minimal
    # containers). Try /etc/timezone, then fall back to the C library name.
    try:
        with open("/etc/timezone") as fh:
            cand = fh.read().strip()
            if cand and _IANA_TIME_ZONE.match(cand):
                return cand
    except Exception:
        pass
    try:
        return time.tzname[0] or "UTC"
    except Exception:
        return "UTC"


def build_time_context_block(
    *,
    turn: int = 1,
    step: int = 1,
    client_time_zone: str | None = None,
    previous_ms: float | None = None,
) -> str:
    """Construct the injectable time-context block for the current turn.

    Falls back to the server process zone when the client did not supply a
    canonical zone. The returned string includes a trailing newline so it can
    be concatenated directly after the [Workspace::v1] prefix.
    """
    resolved_client = client_time_zone if client_time_zone is not None else get_client_timezone()
    zone = _validate_time_zone(resolved_client) or server_default_time_zone()
    block = render_time_context(
        turn=turn,
        step=step,
        now_ms=__import__("time").time() * 1000.0,
        previous_ms=previous_ms,
        time_zone=zone,
    )
    return f"\n{block}\n"
