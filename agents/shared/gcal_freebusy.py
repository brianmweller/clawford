"""Google Calendar freebusy helpers — pure request/response shaping
plus a thin wrapper that calls the API.

Separated from caller scripts so the shaping is testable without a
calendar service. The API call itself is trivial — just passes through
to service.freebusy().query() — but the request-body assembly and
response flattening (merging busy across multiple calendars, stripping
overlaps) belong in one place.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _iso_utc(dt: datetime) -> str:
    """Calendar freebusy requires RFC 3339 with explicit offset. Normalize
    any tz-aware datetime to a UTC 'Z'-suffixed ISO string."""
    if dt.tzinfo is None:
        raise ValueError("gcal_freebusy requires tz-aware datetimes")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_freebusy_request_body(
    start: datetime,
    end: datetime,
    calendar_ids: list[str] | None = None,
) -> dict:
    """Shape a freebusy().query body.

    calendar_ids defaults to ['primary']. Pass multiple IDs (e.g. work +
    personal) to get a merged busy view.
    """
    ids = calendar_ids or ["primary"]
    return {
        "timeMin": _iso_utc(start),
        "timeMax": _iso_utc(end),
        "items": [{"id": cid} for cid in ids],
    }


def parse_freebusy_response(resp: dict) -> list[dict]:
    """Flatten a freebusy response into a sorted, deduped list of
    {"start": ISO, "end": ISO} blocks across all queried calendars.

    Overlapping intervals are merged so the LLM sees one contiguous
    busy window rather than fragmented slices.
    """
    raw = []
    for cal in (resp.get("calendars") or {}).values():
        for b in cal.get("busy") or []:
            if "start" in b and "end" in b:
                raw.append((b["start"], b["end"]))
    if not raw:
        return []
    raw.sort()
    merged: list[tuple[str, str]] = [raw[0]]
    for s, e in raw[1:]:
        last_s, last_e = merged[-1]
        if s <= last_e:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))
    return [{"start": s, "end": e} for s, e in merged]


def query_busy(
    service,
    start: datetime,
    end: datetime,
    calendar_ids: list[str] | None = None,
) -> list[dict]:
    """Call the Calendar freebusy endpoint and return merged busy blocks.

    Never raises on API errors — returns [] on failure so draft-compose
    falls through to freehand scheduling instead of blowing up the cron.
    """
    try:
        body = build_freebusy_request_body(start, end, calendar_ids)
        resp = service.freebusy().query(body=body).execute()
        return parse_freebusy_response(resp)
    except Exception:  # noqa: BLE001
        return []
